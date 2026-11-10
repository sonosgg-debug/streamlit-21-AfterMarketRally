"""
Data Loader Module for AfterMarket Rally Screener
Complies with 00 App_AI_Template Guides:
- Guide 04: Timezone (KST) & Trading Day Safety
- Guide 05: Global Socket Timeout (5.0s) & Multi-tier Fallback
- Guide 06: Data Integrity & Defensive APIs
"""

import os
import sys
import re
import json
from datetime import datetime, timedelta
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor
from config import KST, KRX_HOLIDAYS, THEME, MAX_HISTORICAL_DAYS

# ---------------------------------------------------------
# 0. KRX 공식 호가 단위(Tick Size) 정합성 검증기
# ---------------------------------------------------------

def get_krx_tick_size(price: int) -> int:
    """KRX/KOSDAQ 공식 호가 단위(Tick Size)를 반환합니다."""
    if price < 2000:
        return 1
    elif price < 5000:
        return 5
    elif price < 20000:
        return 10
    elif price < 50000:
        return 50
    elif price < 200000:
        return 100
    elif price < 500000:
        return 500
    else:
        return 1000

def align_to_krx_tick(price: float) -> int:
    """주어진 가격을 가장 가까운 KRX 정규 호가 단위로 정렬합니다."""
    if not price or price <= 0:
        return 0
    p = round(price)
    tick = get_krx_tick_size(p)
    return int(round(p / tick) * tick)

# ---------------------------------------------------------
# 1. 영업일 및 시장 운영 세션 판정 (가이드 04 준수)
# ---------------------------------------------------------

def is_krx_trading_day(d: datetime.date) -> bool:
    """주어진 날짜가 실제 KRX 개장 거래일인지 판정합니다."""
    d_str = d.strftime("%Y%m%d")
    if d.weekday() >= 5:  # 주말
        return False
    if d_str in KRX_HOLIDAYS:  # 법정 공휴일
        return False
    return True

def get_latest_expected_trading_day() -> str:
    """
    현재 시각 기준으로 가장 최근의 유효 거래일 문자열(YYYY-MM-DD)을 안전하게 산출합니다.
    """
    now_kst = datetime.now(KST)
    today = now_kst.date()

    if now_kst.weekday() < 5 and not (now_kst.hour < 9 or (now_kst.hour == 15 and now_kst.minute < 30)):
        if is_krx_trading_day(today):
            return today.strftime("%Y-%m-%d")

    d = today - timedelta(days=1)
    for _ in range(60):
        if is_krx_trading_day(d):
            return d.strftime("%Y-%m-%d")
        d -= timedelta(days=1)

    return (today - timedelta(days=1)).strftime("%Y-%m-%d")

def get_previous_trading_day(base_date=None) -> str:
    """
    기준 날짜(기본값: 오늘) 직전의 유효 KRX 거래일 문자열(YYYY-MM-DD)을 반환합니다.
    """
    now_kst = datetime.now(KST)
    b_date = base_date or now_kst.date()
    d = b_date - timedelta(days=1)
    for _ in range(60):
        if is_krx_trading_day(d):
            return d.strftime("%Y-%m-%d")
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def get_available_dates(max_days: int = MAX_HISTORICAL_DAYS) -> list:
    """
    로컬 및 원격 저장소에서 사용 가능한 스냅샷/애프터마켓 거래일 목록(YYYY-MM-DD)을
    영업일 기준 최근 5일(max_days) 이내로 제한하여 내림차순으로 반환합니다.
    """
    snapshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    dates = set()

    # 1. 로컬 스냅샷 파일 탐색
    if os.path.exists(snapshots_dir):
        for fname in os.listdir(snapshots_dir):
            m = re.match(r"^snapshot_(\d{8})_1530\.json$", fname)
            if m:
                d_str = m.group(1)
                try:
                    formatted = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]}"
                    dates.add(formatted)
                except Exception:
                    pass

    # 2. KRX 캘린더 기반 최근 유효 거래일 역추적
    now_kst = datetime.now(KST)
    cur_d = now_kst.date()
    if now_kst.hour < 15 or (now_kst.hour == 15 and now_kst.minute < 30):
        cur_d = cur_d - timedelta(days=1)

    cal_days = 0
    while cal_days < (max_days + 2):
        if is_krx_trading_day(cur_d):
            dates.add(cur_d.strftime("%Y-%m-%d"))
            cal_days += 1
        cur_d -= timedelta(days=1)

    # 3. 2026-10-07 이후의 공식 유효 데이터만 필터링
    valid_dates = [d for d in dates if d >= "2026-10-07"]
    sorted_dates = sorted(valid_dates, reverse=True)

    return sorted_dates[:max_days]

def get_default_date_mode() -> tuple:
    """
    현재 KST 시각 기준 최적의 기본 조회 모드('LIVE' 또는 'DATE') 및 기본 날짜(YYYY-MM-DD)를 반환합니다.
    - 15:40 ~ 20:00: 실시간 애프터마켓 모드 ('LIVE', today_str)
    - 20:00 이후 (장 마감): 당일 애프터마켓 최종 마감 확정 모드 ('DATE', today_str)
    - 00:00 ~ 15:39: 직전 거래일 마감 모드 ('DATE', prev_day)
    """
    now_kst = datetime.now(KST)
    today = now_kst.date()
    is_open = is_krx_trading_day(today)
    today_str = today.strftime("%Y-%m-%d")

    available = get_available_dates()
    prev_day = get_previous_trading_day(today)

    if is_open:
        hour = now_kst.hour
        minute = now_kst.minute
        # 15:40 ~ 20:00 사이: 실시간 애프터마켓 모드
        if (hour == 15 and minute >= 40) or (16 <= hour < 20):
            return "LIVE", today_str
        # 20:00 이후: 당일 애프터마켓 최종 마감 확정 모드 (오늘 날짜의 확정 마감 데이터 제공)
        elif hour >= 20:
            return "DATE", today_str

    # 그 외 모든 시간대 (아침 개장 전, 정규장 중, 휴장일)는 직전 거래일 확정 모드
    return "DATE", prev_day

def load_market_close_snapshot(target_date: str = None) -> dict:
    """
    15:30 정규장 마감 및 애프터마켓 스냅샷 데이터를 안전하게 로드합니다.
    - target_date가 지정된 경우(과거/특정 일자 조회):
      * 해당 날짜(YYYYMMDD)와 일치하는 스냅샷만 엄격히 로드
    - target_date가 None 또는 'LIVE' (실시간 LIVE 모드):
      * 오늘(Today KST)이 KRX 거래일이고 15:30 이후인 경우 당일 스냅샷 탐색
      * 당일 스냅샷이 로컬/원격 모두 부재 시 snapshot_collector를 통한 즉시 온디맨드 자동 수집·저장
    """
    snapshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)

    now_kst = datetime.now(KST)
    today = now_kst.date()
    today_str = today.strftime("%Y%m%d")

    # -------------------------------------------------------------
    # Case A: 특정 거래일 지정 로드
    # -------------------------------------------------------------
    if target_date and target_date != "LIVE":
        req_d = target_date.replace("-", "")
        if req_d < "20261007":
            return {}

        date_file = os.path.join(snapshots_dir, f"snapshot_{req_d}_1530.json")

        def _is_healthy_snapshot(snap: dict) -> bool:
            """스냅샷 데이터가 손상되지 않고 실제 거래 데이터를 온전히 담고 있는지 검증합니다."""
            if not snap or not isinstance(snap, dict):
                return False
            stocks = snap.get("stocks", {})
            if not stocks or len(stocks) < 100:
                return False
            sample = list(stocks.values())[:30]
            if not all("over_price" in s for s in sample):
                return False
            # 마감된 세션(과거일 또는 당일 20:00 이후)인데 시간외 거래량이 전 종목 0이면 조기 수집된 손상 파일로 간주
            is_completed = (req_d < today_str) or (req_d == today_str and now_kst.hour >= 20)
            if is_completed and sum(s.get("after_volume", 0) for s in sample) == 0:
                return False
            return True

        # 1. 로컬 특정 일자 파일 우선 확인
        if os.path.exists(date_file):
            try:
                with open(date_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data and str(data.get("date", "")).replace("-", "") == req_d:
                        if _is_healthy_snapshot(data):
                            return data
                        else:
                            print(f"[!] Local snapshot for {req_d} is premature or corrupted (after_volume=0). Proceeding to self-heal...")
            except Exception:
                pass

        # 2. 로컬 latest_snapshot.json 확인 (내부 date가 정확히 req_d와 일치할 때만)
        latest_file = os.path.join(snapshots_dir, "latest_snapshot.json")
        if os.path.exists(latest_file):
            try:
                with open(latest_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data and str(data.get("date", "")).replace("-", "") == req_d:
                        if _is_healthy_snapshot(data):
                            return data
            except Exception:
                pass

        # 3. 원격 GitHub Raw에서 해당 날짜 파일 1:1 다운로드 시도
        g_url = f"https://raw.githubusercontent.com/sonosgg-debug/streamlit-21-AfterMarketRally/main/snapshots/snapshot_{req_d}_1530.json"
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(g_url, headers=headers, timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                if data and str(data.get("date", "")).replace("-", "") == req_d:
                    if _is_healthy_snapshot(data):
                        try:
                            with open(date_file, "w", encoding="utf-8") as f:
                                json.dump(data, f, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
                        return data
        except Exception:
            pass

        # 4. 자가 치유(Self-Healing): 유효한 스냅샷이 없거나 손상된 경우 즉시 온디맨드로 재구축 및 복원
        try:
            import snapshot_collector
            print(f"[*] On-demand collecting and self-healing snapshot for {req_d}...")
            healed_file = snapshot_collector.collect_snapshot(target_date_str=req_d, force=True)
            if healed_file and os.path.exists(healed_file):
                with open(healed_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data and "stocks" in data and data["stocks"]:
                        return data
        except Exception as e:
            print(f"[Warning] Failed to self-heal snapshot for {req_d}: {e}")

        return {}

    # -------------------------------------------------------------
    # Case B: 실시간(LIVE) 모드 당일 스냅샷 로드
    # -------------------------------------------------------------
    is_today_open = is_krx_trading_day(today)
    is_after_market_close = (now_kst.hour > 15 or (now_kst.hour == 15 and now_kst.minute >= 30))

    if not (is_today_open and is_after_market_close):
        return {}

    today_file = os.path.join(snapshots_dir, f"snapshot_{today_str}_1530.json")

    # 1. 로컬 당일 스냅샷 파일 탐색
    if os.path.exists(today_file):
        try:
            with open(today_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and str(data.get("date", "")).replace("-", "") == today_str:
                    if "stocks" in data and data["stocks"]:
                        return data
        except Exception:
            pass

    # 2. 로컬 latest_snapshot.json 날짜 검증
    latest_file = os.path.join(snapshots_dir, "latest_snapshot.json")
    if os.path.exists(latest_file):
        try:
            with open(latest_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and str(data.get("date", "")).replace("-", "") == today_str:
                    if "stocks" in data and data["stocks"]:
                        return data
        except Exception:
            pass

    # 3. 원격 GitHub에서 당일 스냅샷 탐색
    g_url = f"https://raw.githubusercontent.com/sonosgg-debug/streamlit-21-AfterMarketRally/main/snapshots/snapshot_{today_str}_1530.json"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(g_url, headers=headers, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            if data and str(data.get("date", "")).replace("-", "") == today_str:
                if "stocks" in data and data["stocks"]:
                    try:
                        with open(today_file, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                    return data
    except Exception:
        pass

    # 4. 자가 치유(Self-Healing): 당일 스냅샷이 전혀 없으면 앱 내부에서 온디맨드로 즉시 생성
    try:
        import snapshot_collector
        print(f"[*] On-demand collecting snapshot for {today_str}...")
        created_file = snapshot_collector.collect_snapshot(target_date_str=today_str, force=True)
        if created_file and os.path.exists(created_file):
            with open(created_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and "stocks" in data and data["stocks"]:
                    return data
    except Exception as e:
        print(f"[Warning] Failed to on-demand collect snapshot: {e}")

    return {}

def get_market_session_info(selected_date: str = "LIVE") -> dict:
    """
    현재 한국 시각(KST) 및 선택된 조회 기준일에 따른 시장 운영 세션을 판별합니다.
    """
    now_kst = datetime.now(KST)
    today = now_kst.date()
    is_open_day = is_krx_trading_day(today)
    time_str = now_kst.strftime("%H:%M:%S")
    date_str = now_kst.strftime("%Y-%m-%d")

    # [과거/직전 거래일 기준 모드]
    if selected_date and selected_date != "LIVE":
        snap_data = load_market_close_snapshot(selected_date)
        snap_date = snap_data.get("date", selected_date.replace("-", ""))
        formatted_snap_date = f"{snap_date[:4]}-{snap_date[4:6]}-{snap_date[6:8]}" if len(snap_date) == 8 else snap_date
        snap_count = len(snap_data.get("stocks", {}))
        has_snap = snap_count > 0
        snap_text = f"📸 {formatted_snap_date} 확정 스냅샷 ({snap_count:,}개 종목 연동)" if has_snap else "⚡ 실시간 직접 집계 모드"

        return {
            "status": "HISTORICAL",
            "badge_text": f"📅 {selected_date} 애프터마켓 확정 마감",
            "badge_color": "#38bdf8",
            "desc": f"조회 기준일({selected_date})의 정규장 종가 및 애프터마켓 실제 체결가 데이터를 표시합니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": selected_date
        }

    # [실시간 애프터마켓 Live 모드]
    snap_data = load_market_close_snapshot()
    snap_date = snap_data.get("date", "")
    snap_count = len(snap_data.get("stocks", {}))
    has_snap = snap_count > 0
    snap_text = f"📸 15:30 정규장 스냅샷: {snap_date} ({snap_count:,}개 종목 연동)" if has_snap else "⚡ 실시간 직접 집계 모드"

    if not is_open_day:
        return {
            "status": "CLOSED",
            "badge_text": "🏛️ 휴장일 (주말/공휴일)",
            "badge_color": "#64748b",
            "desc": "오늘은 거래소 휴장일입니다. (직전 영업일 확정 시세를 조회하시려면 조회 기준일을 선택하세요)",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }

    hour = now_kst.hour
    minute = now_kst.minute

    if hour < 9:
        return {
            "status": "PRE_MARKET",
            "badge_text": "🌙 장 개장 전 (00:00~08:59)",
            "badge_color": "#64748b",
            "desc": "정규장 개장(09:00) 전입니다. (어제 애프터마켓 데이터를 보시려면 조회 기준일에서 직전 거래일을 선택하세요)",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }
    elif hour < 15 or (hour == 15 and minute <= 30):
        return {
            "status": "REGULAR_MARKET",
            "badge_text": "🟢 정규장 진행 중 (09:00~15:30)",
            "badge_color": "#22c55e",
            "desc": "정규 거래 시간입니다. 15:30 정규장 마감 후 애프터마켓이 시작됩니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }
    elif hour == 15 and minute > 30 and minute < 40:
        return {
            "status": "PRE_AFTERMARKET",
            "badge_text": "⏳ 정규장 마감 동시호가 집계 중 (15:30~15:40)",
            "badge_color": "#eab308",
            "desc": "15:35 정규장 마감 스냅샷이 기록되며, 15:40 NXT 애프터마켓이 개장합니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }
    elif hour == 15 and minute >= 40:
        return {
            "status": "NXT_AFTERMARKET",
            "badge_text": "⚡ NXT 애프터마켓 선행 진행 중 (15:40~)",
            "badge_color": "#eab308",
            "desc": "대체거래소(NXT) 애프터마켓이 선행 개장되었습니다. 16:00부터 KRX가 개장합니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": True,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }
    elif 16 <= hour < 20:
        return {
            "status": "AFTERMARKET_LIVE",
            "badge_text": "🔥 KRX & NXT 애프터마켓 실시간 진행 중 (16:00~20:00)",
            "badge_color": "#f97316",
            "desc": "정규장 종가 대비 실시간 시간외 체결가·거래량 급변 종목을 정밀 분석 중입니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": True,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }
    else:
        return {
            "status": "AFTERMARKET_CLOSED",
            "badge_text": "🏁 당일 애프터마켓 최종 마감 (20:00 확정)",
            "badge_color": "#3b82f6",
            "desc": "당일 정규장 및 애프터마켓 거래가 모두 최종 마감되어 확정 집계되었습니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False,
            "snapshot_text": snap_text,
            "has_snapshot": has_snap,
            "selected_date": "LIVE"
        }

# ---------------------------------------------------------
# 2. 유니버스 확보: 시가총액 상위 리스트 고속 수집
# ---------------------------------------------------------

def fetch_universe_by_scope(
    market: str = "전체 (ALL)",
    scope: str = "시총 상위 200 (쾌속 모드)",
    **kwargs
) -> list:
    """
    네이버 증권 시가총액 API를 호출하여 대상 종목 코드 및 시가총액(억 원)을 초고속 수집합니다.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    
    if market == "코스피 (KOSPI)":
        target_markets = ["KOSPI"]
    elif market == "코스닥 (KOSDAQ)":
        target_markets = ["KOSDAQ"]
    else:
        target_markets = ["KOSPI", "KOSDAQ"]

    if ("200" in scope or "200개" in scope) and "코스피" not in scope:
        limit_per_market = 200 if len(target_markets) == 1 else 100
    elif "500" in scope or "500개" in scope:
        limit_per_market = 500 if len(target_markets) == 1 else 250
    elif "코스피 200" in scope:
        limit_per_market = 200 if len(target_markets) == 1 else 150
    else:
        limit_per_market = 300

    universe = []
    for m in target_markets:
        pages_needed = (limit_per_market + 99) // 100
        for p in range(1, pages_needed + 1):
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{m}?page={p}&pageSize=100"
            try:
                r = requests.get(url, headers=headers, timeout=5.0)
                if r.status_code == 200:
                    stocks = r.json().get("stocks", [])
                    for s in stocks:
                        code = s.get("itemCode")
                        name = s.get("stockName")
                        m_val_raw = s.get("marketValueRaw", 0)
                        try:
                            marcap = int(int(m_val_raw) / 100_000_000)
                        except (ValueError, TypeError):
                            marcap = 0

                        universe.append({
                            "Code": str(code).zfill(6),
                            "Name": name,
                            "Market": "KOSPI" if m == "KOSPI" else "KOSDAQ",
                            "MarketCap": marcap
                        })
                if len(stocks) < 100:
                    break
            except Exception:
                pass

    return universe

# ---------------------------------------------------------
# 3. 네이버 증권 & 다음 금융 상호보완적 시세 수집기
# ---------------------------------------------------------

def _fetch_naver_batch(code_chunk: list) -> dict:
    """
    네이버 증권 실시간 폴링 API: KRX 누적 거래량/대금, NXT 누적 거래량/대금, 실제 시간외 체결가 수집
    """
    if not code_chunk:
        return {}

    codes_str = ",".join(code_chunk)
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{codes_str}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://m.stock.naver.com/"
    }

    result = {}
    try:
        r = requests.get(url, headers=headers, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            items = data.get("datas", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("itemCode", "")).zfill(6)
                name = item.get("stockName", "")
                market = "KOSPI" if item.get("stockExchangeType", {}).get("code") == "KS" else "KOSDAQ"

                # KRX 누적 거래량 & 거래대금
                c_vol = int(item.get("accumulatedTradingVolumeRaw", 0) or 0)
                c_val = int(item.get("accumulatedTradingValueRaw", 0) or 0)

                # NXT 누적 거래량 & 거래대금 & 실제 시간외 체결가
                over_info = item.get("overMarketPriceInfo", {}) or {}
                d_vol = int(over_info.get("accumulatedTradingVolumeRaw", 0) or 0)
                nxt_val_raw = over_info.get("accumulatedTradingValueRaw")
                try:
                    nxt_val = int(nxt_val_raw or 0)
                except (ValueError, TypeError):
                    nxt_val = 0

                over_price_raw = over_info.get("overPrice")
                try:
                    over_price = int(str(over_price_raw).replace(",", "")) if over_price_raw else 0
                except (ValueError, TypeError):
                    over_price = 0

                # 통합(KRX+NXT) 거래량 & 거래대금
                integ_info = item.get("integratedPriceInfo", {}) or {}
                integ_vol_raw = integ_info.get("accumulatedTradingVolumeRaw")
                integ_val_raw = integ_info.get("accumulatedTradingValueRaw")
                total_vol = int(integ_vol_raw) if integ_vol_raw else (c_vol + d_vol)
                total_val = int(integ_val_raw) if integ_val_raw else (c_val + nxt_val)

                # 네이버 종가
                naver_close = int(item.get("closePriceRaw", 0) or 0)

                result[code] = {
                    "name": name,
                    "market": market,
                    "krx_vol": c_vol,
                    "krx_val": c_val,
                    "nxt_vol": d_vol,
                    "nxt_val": nxt_val,
                    "total_vol": total_vol,
                    "total_val": total_val,
                    "naver_close": naver_close,
                    "over_price": over_price
                }
    except Exception:
        pass

    return result

def _fetch_daum_quote(code: str) -> tuple:
    """
    다음(Daum) 금융 API: KRX 정규장 공식 종가(regularTradePrice) 및 시간외 실제 체결가(tradePrice) 정밀 수집
    """
    url = f"https://finance.daum.net/api/quotes/A{code}?summary=false&changeOverMarket=true"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        "Referer": "https://finance.daum.net/"
    }
    try:
        r = requests.get(url, headers=headers, timeout=4.0)
        if r.status_code == 200:
            d = r.json()
            reg_price = d.get("regularTradePrice")
            trade_price = d.get("tradePrice")
            return code, reg_price, trade_price
    except Exception:
        pass
    return code, None, None

def build_historical_screener_data(
    target_date: str,
    market: str = "전체 (ALL)",
    scope: str = "시총 상위 200 (쾌속 모드)",
    min_market_cap: int = 3000,
    min_vol_ratio: float = 0.0,
    min_after_val: int = 0
) -> pd.DataFrame:
    """
    지정된 과거/직전 거래일(target_date, YYYY-MM-DD)의 확정 스냅샷으로부터
    정규장 및 애프터마켓 실제 체결가(호가 단위 정합)와 수급 데이터를 정밀 복원하여 반환합니다.
    """
    snapshot = load_market_close_snapshot(target_date)
    snap_stocks = snapshot.get("stocks", {}) if snapshot else {}
    if not snap_stocks:
        return pd.DataFrame()

    records = []
    for code, s in snap_stocks.items():
        name = s.get("name", "")
        mkt = s.get("market", "")
        cap = s.get("market_cap", 0)

        # 1. 시장 필터링
        if market == "코스피 (KOSPI)" and mkt != "KOSPI":
            continue
        if market == "코스닥 (KOSDAQ)" and mkt != "KOSDAQ":
            continue

        # 2. 최소 시가총액 필터링
        if min_market_cap > 0 and cap < min_market_cap:
            continue

        close_price = int(s.get("close_price", 0) or 0)
        krx_vol = int(s.get("krx_volume", 0) or 0)
        krx_val = int(s.get("krx_value", 0) or 0)
        nxt_vol = int(s.get("nxt_volume", 0) or 0)
        nxt_val = int(s.get("nxt_value", 0) or 0)
        tot_vol = int(s.get("total_volume", 0) or 0)
        tot_val = int(s.get("total_value", 0) or 0)

        # 정규장 거래량 vs 시간외 거래량/대금 산출
        stored_reg_vol = s.get("reg_volume")
        stored_after_vol = s.get("after_volume")
        stored_after_val = s.get("after_value")
        stored_nxt_ratio = s.get("nxt_after_ratio")

        # [시간외 현재가 결정 로직 전면 개선: 실제 체결 호가 우선 + 호가 단위 엄격 준수]
        stored_over_price = s.get("over_price")
        if stored_over_price and stored_over_price > 0:
            over_price = align_to_krx_tick(stored_over_price)
        elif s.get("after_value") and s.get("after_volume"):
            over_price = align_to_krx_tick(s["after_value"] / s["after_volume"])
        else:
            over_price = close_price

        # 확정 스냅샷 내 사전 산출된 시간외 수치 우선 적용
        if stored_after_vol is not None and stored_nxt_ratio is not None:
            reg_vol = int(stored_reg_vol) if stored_reg_vol else (krx_vol if krx_vol > 0 else tot_vol)
            after_vol = int(stored_after_vol)
            after_val = int(stored_after_val) if stored_after_val else int(after_vol * over_price)
            nxt_after_ratio = float(stored_nxt_ratio)
        else:
            reg_vol = krx_vol if krx_vol > 0 else tot_vol
            after_vol = max(0, tot_vol - krx_vol)
            after_val = max(0, tot_val - krx_val)
            nxt_after_ratio = round((nxt_vol / tot_vol) * 100, 2) if tot_vol > 0 else 0.0

        # 시간외 등락률 계산
        stored_change_rate = s.get("over_change_rate")
        if stored_change_rate is not None and (stored_over_price and stored_over_price > 0):
            over_change_rate = float(stored_change_rate)
        elif close_price > 0 and over_price > 0:
            over_change_rate = round(((over_price - close_price) / close_price) * 100, 2)
        else:
            over_change_rate = 0.0

        # 시간외 거래량 비율(%)
        vol_ratio = round((after_vol / reg_vol) * 100, 2) if reg_vol > 0 else 0.0

        # 진성 수급 조건
        is_real_rally = (vol_ratio >= 0.1 and after_val >= 50_000_000)

        records.append({
            "종목코드": code,
            "종목명": name,
            "시장": mkt,
            "시가총액(억)": int(cap) if cap else 0,
            "정규장 종가": close_price,
            "시간외 현재가": over_price,
            "시간외 등락률(%)": over_change_rate,
            "정규장 거래량": reg_vol,
            "시간외 거래량": after_vol,
            "시간외 거래대금": after_val,
            "시간외 거래대금(억)": round(after_val / 100_000_000, 1),
            "시간외 거래량 비율(%)": vol_ratio,
            "시간외 NXT 비중(%)": nxt_after_ratio,
            "진성수급": is_real_rally
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # 3. 대상 범위(Scope) 필터링
    if ("200" in scope or "200개" in scope) and "코스피" not in scope:
        limit_cnt = 200
        df = df.sort_values(by="시가총액(억)", ascending=False).head(limit_cnt)
    elif "500" in scope or "500개" in scope:
        limit_cnt = 500
        df = df.sort_values(by="시가총액(억)", ascending=False).head(limit_cnt)
    elif "코스피 200" in scope:
        df_ks = df[df["시장"] == "KOSPI"].sort_values(by="시가총액(억)", ascending=False).head(200)
        df_kq = df[df["시장"] == "KOSDAQ"].sort_values(by="시가총액(억)", ascending=False).head(150)
        df = pd.concat([df_ks, df_kq])

    # 4. 추가 수급 필터링
    if min_vol_ratio > 0:
        df = df[df["시간외 거래량 비율(%)"] >= min_vol_ratio]
    if min_after_val > 0:
        df = df[df["시간외 거래대금"] >= (min_after_val * 100_000_000)]

    # 기본 정렬: 시간외 등락률(%) 내림차순
    df = df.sort_values(by="시간외 등락률(%)", ascending=False).reset_index(drop=True)
    return df

def get_screener_data(
    market: str = "전체 (ALL)",
    scope: str = "시총 상위 200 (쾌속 모드)",
    min_market_cap: int = 3000,
    min_vol_ratio: float = 0.0,
    min_after_val: int = 0,
    target_date: str = "LIVE",
    **kwargs
) -> pd.DataFrame:
    """
    15:30 정규장 마감 및 애프터마켓 실제 체결가(호가) 데이터를 정합 분석하여
    정규장 대비 시간외 급변 종목을 정밀 발굴합니다.
    - target_date == "LIVE" 또는 오늘 날짜: 실시간 API를 직접 로드하여 100% 공식 종가 및 마감 체결 호가 반영
    - 과거 일자 조회: 스냅샷 파일 기반 복원
    """
    now_kst = datetime.now(KST)
    today_str = now_kst.date().strftime("%Y-%m-%d")

    # 과거 다른 일자(어제 이하)를 명시적으로 선택한 경우만 스냅샷 파일 기반 복원
    if target_date and target_date != "LIVE" and target_date != today_str:
        return build_historical_screener_data(
            target_date=target_date,
            market=market,
            scope=scope,
            min_market_cap=min_market_cap,
            min_vol_ratio=min_vol_ratio,
            min_after_val=min_after_val
        )

    # 1. 유니버스 확보
    universe = fetch_universe_by_scope(market=market, scope=scope)
    if not universe:
        return pd.DataFrame()

    u_df = pd.DataFrame(universe)
    if min_market_cap > 0:
        u_df = u_df[u_df["MarketCap"] >= min_market_cap]

    if u_df.empty:
        return pd.DataFrame()

    codes = u_df["Code"].tolist()
    cap_map = dict(zip(u_df["Code"], u_df["MarketCap"]))

    # 2. 당일 스냅샷 로드
    snapshot = load_market_close_snapshot()
    today_clean = now_kst.strftime("%Y%m%d")
    snap_is_today = (str(snapshot.get("date", "")).replace("-", "") == today_clean)
    snap_stocks = snapshot.get("stocks", {}) if (snapshot and snap_is_today) else {}

    # 3. 네이버 배치 시세 병렬 수집
    chunk_size = 25
    chunks = [codes[i:i + chunk_size] for i in range(0, len(codes), chunk_size)]

    naver_data = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_fetch_naver_batch, chunk) for chunk in chunks]
        for f in futures:
            res = f.result()
            if res:
                naver_data.update(res)

    # 4. 다음 금융 병렬 수집 (KRX 정규장 공식 종가 & 실제 시간외 체결가 확보)
    daum_data = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        daum_results = list(executor.map(_fetch_daum_quote, codes))
        for code, reg_p, trade_p in daum_results:
            daum_data[code] = (reg_p, trade_p)

    # 5. 데이터 통합 및 상호 보완 가공 (호가 단위 엄격 정합)
    records = []
    for code in codes:
        n_info = naver_data.get(code, {})
        d_info = daum_data.get(code, (None, None))
        snap = snap_stocks.get(code, {})

        name = n_info.get("name", snap.get("name", ""))
        mkt = n_info.get("market", snap.get("market", ""))
        cap = cap_map.get(code, snap.get("market_cap", 0))

        # (1) 정규장 공식 종가 결정 (Daum API 공식 종가 최우선)
        daum_reg, daum_trade = d_info
        naver_close = n_info.get("naver_close", 0)
        naver_over = n_info.get("over_price", 0)
        snap_close = snap.get("close_price") if snap_is_today else None

        if daum_reg and daum_reg > 0:
            reg_price = int(daum_reg)
        elif snap_close and snap_close > 0:
            reg_price = int(snap_close)
        elif naver_close and naver_close > 0:
            reg_price = int(naver_close)
        else:
            reg_price = 0

        # (2) 시간외 실제 체결가(호가) 결정
        # 1순위: Daum tradePrice (실시간 시간외 체결가)
        # 2순위: Naver overMarketPriceInfo overPrice
        # 3순위: 스냅샷 over_price
        # 4순위: reg_price
        snap_over = snap.get("over_price") if snap_is_today else None

        if daum_trade and daum_trade > 0:
            over_price = int(daum_trade)
        elif naver_over and naver_over > 0:
            over_price = int(naver_over)
        elif snap_over and snap_over > 0:
            over_price = int(snap_over)
        else:
            over_price = reg_price

        # KRX 공식 호가 단위 정렬 가드
        if over_price > 0:
            over_price = align_to_krx_tick(over_price)

        # (3) 시간외 등락률(%) = {(시간외 현재가 - 정규장 종가) / 정규장 종가} * 100
        if reg_price > 0 and over_price > 0:
            over_change_rate = round(((over_price - reg_price) / reg_price) * 100, 2)
        else:
            over_change_rate = 0.0

        # (4) 실시간 누적 거래량 & 거래대금
        live_krx_vol = n_info.get("krx_vol", 0)
        live_nxt_vol = n_info.get("nxt_vol", 0)
        live_total_vol = n_info.get("total_vol", live_krx_vol + live_nxt_vol)

        live_krx_val = n_info.get("krx_val", 0)
        live_nxt_val = n_info.get("nxt_val", 0)
        live_total_val = n_info.get("total_val", live_krx_val + live_nxt_val)

        # (5) 시간외 거래량/거래대금 산출
        snap_reg_vol = snap.get("reg_volume")
        snap_after_vol = snap.get("after_volume")
        snap_after_val = snap.get("after_value")
        snap_nxt_ratio = snap.get("nxt_after_ratio")
        snap_after_nxt = snap.get("after_nxt_volume")

        snap_total_vol = snap.get("total_volume", 0) if snap_is_today else 0
        snap_total_val = snap.get("total_value", 0) if snap_is_today else 0
        snap_nxt_vol = snap.get("nxt_volume", 0) if snap_is_today else 0
        snap_krx_vol = snap.get("krx_volume", 0) if snap_is_today else 0

        # Case 1: 스냅샷에 당일 확정 시간외 수치가 이미 완비되어 있는 경우 (20:00 마감 후 확정 스냅샷)
        if snap_is_today and snap_after_vol is not None and snap_nxt_ratio is not None:
            reg_vol = int(snap_reg_vol) if snap_reg_vol and snap_reg_vol > 0 else (snap_krx_vol if snap_krx_vol > 0 else snap_total_vol)
            after_vol = int(snap_after_vol)
            after_nxt_vol = int(snap_after_nxt) if snap_after_nxt is not None else int(round(after_vol * (snap_nxt_ratio / 100.0)))
            after_val = int(snap_after_val) if snap_after_val else int(after_vol * over_price)
            nxt_after_ratio = float(snap_nxt_ratio)
        elif snap_is_today and snap_total_vol > 0:
            # Case 2: 15:30 정규장 마감 스냅샷 기준 실시간 차감 계산 (15:40~20:00 장중 및 실시간)
            reg_vol = snap_total_vol
            after_krx = max(0, live_krx_vol - snap_krx_vol) if snap_krx_vol > 0 else max(0, live_total_vol - snap_total_vol)
            after_nxt_vol = max(0, live_nxt_vol - snap_nxt_vol)
            after_vol = after_krx + after_nxt_vol
            after_val = max(0, live_total_val - snap_total_val)
            nxt_after_ratio = round((after_nxt_vol / after_vol) * 100, 2) if after_vol > 0 else 0.0
        else:
            # Case 3: 스냅샷 부재 시 (오류 방지를 위해 0으로 정규화)
            reg_vol = live_total_vol
            after_vol = 0
            after_val = 0
            after_nxt_vol = 0
            nxt_after_ratio = 0.0

        if after_val == 0 and after_vol > 0 and over_price > 0:
            after_val = after_vol * over_price

        # (6) 시간외 거래량 비율(%) = {시간외 거래량 / 정규장 거래량} * 100
        vol_ratio = round((after_vol / reg_vol) * 100, 2) if reg_vol > 0 else 0.0

        # (7) 시간외 NXT 비중(%) 안전 가드
        if after_vol == 0:
            nxt_after_ratio = 0.0

        # (8) 진성 수급 조건: 시간외 거래량 비율 0.1% 이상 & 시간외 거래대금 5천만원 이상
        is_real_rally = (vol_ratio >= 0.1 and after_val >= 50_000_000)

        records.append({
            "종목코드": code,
            "종목명": name,
            "시장": mkt,
            "시가총액(억)": int(cap) if cap else 0,
            "정규장 종가": reg_price,
            "시간외 현재가": over_price,
            "시간외 등락률(%)": over_change_rate,
            "정규장 거래량": reg_vol,
            "시간외 거래량": after_vol,
            "시간외 거래대금": after_val,
            "시간외 거래대금(억)": round(after_val / 100_000_000, 1),
            "시간외 거래량 비율(%)": vol_ratio,
            "시간외 NXT 비중(%)": nxt_after_ratio,
            "진성수급": is_real_rally
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # 6. 추가 수급 필터링
    if min_vol_ratio > 0:
        df = df[df["시간외 거래량 비율(%)"] >= min_vol_ratio]
    if min_after_val > 0:
        df = df[df["시간외 거래대금"] >= (min_after_val * 100_000_000)]

    # 기본 정렬: 시간외 등락률(%) 내림차순
    df = df.sort_values(by="시간외 등락률(%)", ascending=False).reset_index(drop=True)
    return df
