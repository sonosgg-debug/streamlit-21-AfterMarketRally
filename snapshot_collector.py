"""
================================================================================
🏛️ 15:30 정규장 마감 및 애프터마켓 스냅샷 수집기 (Snapshot Collector)
- 실행 시점: 월~금 매일 한국 시각 15:35 (UTC 06:35) 및 장 마감 후
- 기능: 시가총액 상위 종목(600개)의 정규장 공식 종가, 시간외 실제 체결가(호가),
        시간외 거래량/거래대금을 네이버 실시간 배치 API를 통해 10초 이내 초고속 정밀 수집 및 저장
- 저장 경로: snapshots/snapshot_YYYYMMDD_1530.json 및 snapshots/latest_snapshot.json
================================================================================
"""

import os
import sys
import json
import time
import threading

from datetime import datetime, timezone, timedelta
import requests
from concurrent.futures import ThreadPoolExecutor

import re
from config import KST, KRX_HOLIDAYS, MAX_HISTORICAL_DAYS

_collector_lock = threading.Lock()

# -----------------------------------------------------------------------------
# 1. KRX 공식 호가 단위 (Tick Size) 유효성 검증기
# -----------------------------------------------------------------------------
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

def is_krx_trading_day(d) -> bool:
    """주어진 날짜가 실제 KRX 개장 거래일인지 판정합니다."""
    d_str = d.strftime("%Y%m%d")
    if d.weekday() >= 5:  # 주말
        return False
    if d_str in KRX_HOLIDAYS:  # 법정 공휴일
        return False
    return True

def get_previous_trading_day(base_date=None) -> str:
    """기준 날짜(기본값: 오늘) 직전의 유효 KRX 거래일 문자열(YYYYMMDD)을 반환합니다."""
    now_kst = datetime.now(KST)
    b_date = base_date or now_kst.date()
    d = b_date - timedelta(days=1)
    for _ in range(60):
        if is_krx_trading_day(d):
            return d.strftime("%Y%m%d")
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

def fetch_universe(limit_per_market: int = 300) -> list:
    """네이버 증권 시가총액 API를 호출하여 코스피/코스닥 상위 종목을 수집합니다."""
    headers = {"User-Agent": "Mozilla/5.0"}
    universe = []
    pages_needed = (limit_per_market + 99) // 100

    for m in ["KOSPI", "KOSDAQ"]:
        for p in range(1, pages_needed + 1):
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{m}?page={p}&pageSize=100"
            try:
                r = requests.get(url, headers=headers, timeout=5.0)
                if r.status_code == 200:
                    stocks = r.json().get("stocks", [])
                    for s in stocks:
                        code = str(s.get("itemCode", "")).zfill(6)
                        name = s.get("stockName", "")
                        m_val_raw = s.get("marketValueRaw", 0)
                        try:
                            marcap = int(int(m_val_raw) / 100_000_000)
                        except (ValueError, TypeError):
                            marcap = 0
                        universe.append({
                            "code": code,
                            "name": name,
                            "market": m,
                            "market_cap": marcap
                        })
                    if len(stocks) < 100:
                        break
            except Exception as e:
                print(f"[Warning] Failed to fetch marketValue page {p} for {m}: {e}")

    return universe

def fetch_batch_quotes(code_chunk: list) -> list:
    """네이버 실시간 배치 API를 호출하여 시세 목록을 반환합니다."""
    if not code_chunk:
        return []
    codes_str = ",".join(code_chunk)
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{codes_str}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://m.stock.naver.com/"
    }
    try:
        r = requests.get(url, headers=headers, timeout=5.0)
        if r.status_code == 200:
            return r.json().get("datas", [])
    except Exception as e:
        print(f"[Warning] Batch fetch error: {e}")
    return []

def fetch_stock_minute_breakdown(code: str, target_date_str: str) -> dict:
    """
    네이버 증권 KRX 및 NXT 1분봉 차트 API를 호출하여
    15:30 정규장 거래량과 15:40~20:00 시간외 거래량, 15:30 정규장 종가, 시간외 최종 체결가를
    정밀 분리 추출합니다. (startDateTime/endDateTime 파라미터 적용으로 날짜 엄격 보장)
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    cutoff_dt = f"{target_date_str}153000"
    try:
        url_krx = f"https://api.stock.naver.com/chart/domestic/item/{code}/minute?startDateTime={target_date_str}0000&endDateTime={target_date_str}2359"
        r1 = requests.get(url_krx, headers=headers, timeout=4.0)
        d1 = r1.json() if r1.status_code == 200 and isinstance(r1.json(), list) else []

        reg_candles = [x for x in d1 if str(x.get("localDateTime", "")).startswith(target_date_str) and x["localDateTime"] <= cutoff_dt]
        aft_candles = [x for x in d1 if str(x.get("localDateTime", "")).startswith(target_date_str) and x["localDateTime"] > cutoff_dt]
        krx_reg = sum(int(x.get("accumulatedTradingVolume", 0) or 0) for x in reg_candles)
        krx_aft = sum(int(x.get("accumulatedTradingVolume", 0) or 0) for x in aft_candles)

        url_nxt = f"https://api.stock.naver.com/chart/domestic/NXT/item/{code}/minute?startDateTime={target_date_str}0000&endDateTime={target_date_str}2359"
        r2 = requests.get(url_nxt, headers=headers, timeout=4.0)
        d2 = r2.json() if r2.status_code == 200 and isinstance(r2.json(), list) else []

        nxt_reg_candles = [x for x in d2 if str(x.get("localDateTime", "")).startswith(target_date_str) and x["localDateTime"] <= cutoff_dt]
        nxt_aft_candles = [x for x in d2 if str(x.get("localDateTime", "")).startswith(target_date_str) and x["localDateTime"] > cutoff_dt]
        nxt_reg = sum(int(x.get("accumulatedTradingVolume", 0) or 0) for x in nxt_reg_candles)
        nxt_aft = sum(int(x.get("accumulatedTradingVolume", 0) or 0) for x in nxt_aft_candles)

        tot_reg = krx_reg + nxt_reg
        tot_aft = krx_aft + nxt_aft
        nxt_ratio = round((nxt_aft / tot_aft * 100), 2) if tot_aft > 0 else 0.0

        # 분봉에서 15:30 정규장 종가 및 19:59 시간외 최종 체결가 추출
        cand_close = int(reg_candles[-1].get("currentPrice", 0) or 0) if reg_candles else 0
        if nxt_aft_candles:
            cand_over = int(nxt_aft_candles[-1].get("currentPrice", 0) or 0)
        elif aft_candles:
            cand_over = int(aft_candles[-1].get("currentPrice", 0) or 0)
        else:
            cand_over = cand_close

        return {
            "reg_volume": tot_reg,
            "after_volume": tot_aft,
            "after_krx_volume": krx_aft,
            "after_nxt_volume": nxt_aft,
            "nxt_after_ratio": nxt_ratio,
            "close_price": cand_close,
            "over_price": cand_over,
            "krx_volume": krx_reg + krx_aft,
            "nxt_volume": nxt_reg + nxt_aft,
            "total_volume": tot_reg + tot_aft
        }
    except Exception:
        return {}

def collect_snapshot(target_date_str: str = None, force: bool = False) -> str:
    """
    15:30 정규장 마감 및 애프터마켓 실제 체결가/수급 스냅샷을 수집하고 snapshots/ 폴더에 JSON 파일로 저장합니다.
    """
    now_kst = datetime.now(KST)

    # 수집 대상 날짜 결정 및 안전성 검증
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, "%Y%m%d").date()
        except Exception:
            target_date = now_kst.date()
            target_date_str = target_date.strftime("%Y%m%d")
    else:
        # target_date_str이 지정되지 않은 경우:
        # 현재 시각이 15:30 이전이면 당일 정규장이 아직 끝나지 않았으므로 직전 거래일로 안전 전환
        if now_kst.hour < 15 or (now_kst.hour == 15 and now_kst.minute < 30):
            target_date_str = get_previous_trading_day(now_kst.date())
            target_date = datetime.strptime(target_date_str, "%Y%m%d").date()
            print(f"[*] Pre-market/Intraday detected ({now_kst.strftime('%H:%M')} < 15:30). Target date adjusted to previous trading day: {target_date_str}")
        else:
            target_date = now_kst.date()
            target_date_str = now_kst.strftime("%Y%m%d")

    # 당일 15:30 이전인데 명시적으로 오늘을 타겟으로 강제하지 않고 호출한 경우 차단 (조기 수집 방지)
    if not force and target_date == now_kst.date() and (now_kst.hour < 15 or (now_kst.hour == 15 and now_kst.minute < 30)):
        print(f"[*] Today's session ({target_date_str}) has not reached 15:30 close yet. Snapshot collection aborted.")
        return None

    snapshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)
    date_file = os.path.join(snapshots_dir, f"snapshot_{target_date_str}_1530.json")
    latest_file = os.path.join(snapshots_dir, "latest_snapshot.json")

    # Early return: 이미 해당 날짜의 유효한 스냅샷이 존재하고, 수급 데이터가 온전히 갖춰져 있으면 기존 파일 반환
    if not force and os.path.exists(date_file):
        try:
            with open(date_file, "r", encoding="utf-8") as f:
                d = json.load(f)
                stocks = d.get("stocks", {})
                if stocks and len(stocks) >= 100:
                    sample_stocks = list(stocks.values())[:50]
                    has_keys = all("over_price" in s and "after_volume" in s and "nxt_after_ratio" in s for s in sample_stocks)
                    
                    # 마감된 세션(과거일 또는 당일 20:00 이후)인 경우, 시간외 거래량이 실제로 존재하는지 무결성 검증
                    is_completed_market = (target_date < now_kst.date()) or (target_date == now_kst.date() and now_kst.hour >= 20)
                    total_aft_vol = sum(s.get("after_volume", 0) for s in sample_stocks)
                    
                    if has_keys:
                        if is_completed_market and total_aft_vol == 0:
                            print(f"[!] Warning: Existing snapshot for {target_date_str} has 0 aftermarket volume (premature/corrupted). Re-collecting with fresh market data...")
                        else:
                            print(f"[*] Snapshot for {target_date_str} already exists with valid aftermarket data ({len(stocks)} stocks). Returning existing file.")
                            return date_file
        except Exception:
            pass

    # 거래일 검증: 주말(토/일) 및 법정 공휴일 수집 절대 차단 (--force 가 명시된 경우만 예외)
    if not force:
        if not is_krx_trading_day(target_date):
            print(f"[*] Target date ({target_date}) is a weekend or KRX holiday. Snapshot collection skipped.")
            return None

    with _collector_lock:
        print(f"[*] Starting Snapshot collection for date: {target_date_str} at {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}...")
        t0 = time.time()

        # 1. 대상 유니버스 수집 (코스피 300 + 코스닥 300 = 600개)
        universe = fetch_universe(limit_per_market=300)
        if not universe:
            print("[Error] Failed to collect stock universe.")
            return None
        print(f"[*] Universe collected: {len(universe)} stocks.")

        codes = [u["code"] for u in universe]
        code_map = {u["code"]: u for u in universe}

        # 2. 네이버 배치 시세 초고속 병렬 수집
        chunk_size = 25
        chunks = [codes[i:i + chunk_size] for i in range(0, len(codes), chunk_size)]
        quotes = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            for res in executor.map(fetch_batch_quotes, chunks):
                quotes.extend(res)

        print(f"[*] Quotes collected: {len(quotes)} items in {time.time() - t0:.2f}s.")

        # 기존 15:30 기준점 스냅샷 존재 여부 확인
        existing_baseline = {}
        if os.path.exists(date_file):
            try:
                with open(date_file, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    if old_data.get("session") == "BASELINE_1530":
                        existing_baseline = old_data.get("stocks", {})
            except Exception:
                pass

        # 수집 시점 판별: 오늘이고 15:30~15:45 사이면 정규장 마감 기준점(BASELINE), 그 이후면 애프터마켓 완료 반영
        is_today = (target_date == now_kst.date())
        is_baseline_window = (is_today and now_kst.hour == 15 and 30 <= now_kst.minute <= 45)

        # 만약 15:30 기준점이 없거나 과거 일자 수집인 경우, 1분봉 차트에서 정밀 분리 추출
        need_minute_reconstruct = (not is_baseline_window and not existing_baseline)
        minute_map = {}
        if need_minute_reconstruct:
            print(f"[*] Reconstructing regular vs aftermarket via minute candle API for date {target_date_str}...")
            with ThreadPoolExecutor(max_workers=25) as executor:
                futures = {executor.submit(fetch_stock_minute_breakdown, c, target_date_str): c for c in codes}
                for f in futures:
                    c = futures[f]
                    try:
                        res = f.result()
                        if res:
                            minute_map[c] = res
                    except Exception:
                        pass
            print(f"[*] Reconstructed minute breakdown for {len(minute_map)} stocks.")

        # 3. 데이터 구조화
        stocks_data = {}
        for item in quotes:
            if not isinstance(item, dict):
                continue
            code = str(item.get("itemCode", "")).zfill(6)
            u_info = code_map.get(code, {})
            name = item.get("stockName", u_info.get("name", ""))
            mkt = u_info.get("market", "KOSPI")
            cap = u_info.get("market_cap", 0)

            # 정규장 종가
            close_price = int(item.get("closePriceRaw", 0) or 0)

            # KRX 누적 거래량 & 거래대금
            krx_vol = int(item.get("accumulatedTradingVolumeRaw", 0) or 0)
            krx_val = int(item.get("accumulatedTradingValueRaw", 0) or 0)

            # NXT 시간외 누적 거래량 & 거래대금
            over_info = item.get("overMarketPriceInfo", {}) or {}
            nxt_vol = int(over_info.get("accumulatedTradingVolumeRaw", 0) or 0)
            nxt_val_raw = over_info.get("accumulatedTradingValueRaw")
            try:
                nxt_val = int(nxt_val_raw or 0)
            except (ValueError, TypeError):
                nxt_val = 0

            # 실제 시간외 체결가(호가) 결정
            over_price_raw = over_info.get("overPrice")
            try:
                over_price = int(str(over_price_raw).replace(",", "")) if over_price_raw else 0
            except (ValueError, TypeError):
                over_price = 0

            # 시간외 거래가 전혀 없는 종목은 정규장 종가로 동기화
            if over_price == 0:
                over_price = close_price

            # 호가 단위 정렬 가드
            if over_price > 0:
                over_price = align_to_krx_tick(over_price)

            # 시간외 등락률 계산
            if close_price > 0 and over_price > 0:
                over_change_rate = round(((over_price - close_price) / close_price) * 100, 2)
            else:
                over_change_rate = 0.0

            # 통합 거래량 & 거래대금
            integ_info = item.get("integratedPriceInfo", {}) or {}
            integ_vol_raw = integ_info.get("accumulatedTradingVolumeRaw")
            integ_val_raw = integ_info.get("accumulatedTradingValueRaw")

            total_vol = int(integ_vol_raw) if integ_vol_raw else (krx_vol + nxt_vol)
            total_val = int(integ_val_raw) if integ_val_raw else (krx_val + nxt_val)

            after_krx_vol = 0
            # 정규장 거래량 vs 시간외 거래량/비중 정밀 산출
            if is_baseline_window:
                reg_vol = total_vol
                after_vol = 0
                after_nxt_vol = 0
                after_krx_vol = 0
                nxt_ratio = 0.0
                after_val = 0
            elif existing_baseline and code in existing_baseline:
                b_st = existing_baseline[code]
                b_krx = b_st.get("krx_volume", 0)
                b_nxt = b_st.get("nxt_volume", 0)
                b_tot = b_st.get("total_volume", 0)
                after_krx_vol = max(0, krx_vol - b_krx)
                after_nxt_vol = max(0, nxt_vol - b_nxt)
                after_vol = after_krx_vol + after_nxt_vol
                reg_vol = b_tot
                nxt_ratio = round((after_nxt_vol / after_vol * 100), 2) if after_vol > 0 else 0.0
                after_val = after_vol * over_price
            elif code in minute_map:
                m_info = minute_map[code]
                reg_vol = m_info["reg_volume"]
                after_vol = m_info["after_volume"]
                after_nxt_vol = m_info["after_nxt_volume"]
                after_krx_vol = m_info.get("after_krx_volume", 0)
                nxt_ratio = m_info["nxt_after_ratio"]
                if m_info.get("close_price") and m_info["close_price"] > 0:
                    close_price = m_info["close_price"]
                if m_info.get("over_price") and m_info["over_price"] > 0:
                    over_price = align_to_krx_tick(m_info["over_price"])
                if close_price > 0 and over_price > 0:
                    over_change_rate = round(((over_price - close_price) / close_price) * 100, 2)
                after_val = after_vol * over_price
                if m_info.get("krx_volume"):
                    krx_vol = m_info["krx_volume"]
                if m_info.get("nxt_volume"):
                    nxt_vol = m_info["nxt_volume"]
                if m_info.get("total_volume"):
                    total_vol = m_info["total_volume"]
            else:
                reg_vol = total_vol
                after_vol = 0
                after_nxt_vol = 0
                nxt_ratio = 0.0
                after_val = 0

            stocks_data[code] = {
                "name": name,
                "market": mkt,
                "market_cap": cap,
                "close_price": close_price,
                "over_price": over_price,
                "over_change_rate": over_change_rate,
                "reg_volume": reg_vol,
                "after_volume": after_vol,
                "after_krx_volume": after_krx_vol,
                "after_nxt_volume": after_nxt_vol,
                "after_value": after_val,
                "nxt_after_ratio": nxt_ratio,
                "krx_volume": krx_vol,
                "krx_value": krx_val,
                "nxt_volume": nxt_vol,
                "nxt_value": nxt_val,
                "total_volume": total_vol,
                "total_value": total_val
            }

        session_name = "BASELINE_1530" if is_baseline_window else ("ENRICHED_FINAL" if existing_baseline else "RECONSTRUCTED_1530_AND_AFTERMARKET")
        snapshot_payload = {
            "date": target_date_str,
            "session": session_name,
            "timestamp": now_kst.isoformat(),
            "total_stocks": len(stocks_data),
            "stocks": stocks_data
        }

        # 4. 파일 저장 (원자적 교체)
        tmp_date_file = date_file + ".tmp"
        with open(tmp_date_file, "w", encoding="utf-8") as f:
            json.dump(snapshot_payload, f, ensure_ascii=False, indent=2)
        if os.path.exists(date_file):
            try:
                os.remove(date_file)
            except Exception:
                pass
        os.replace(tmp_date_file, date_file)

        # latest_snapshot.json 은 대상 날짜가 기존 latest 날짜 이상인 경우에만 갱신
        existing_latest_date = ""
        if os.path.exists(latest_file):
            try:
                with open(latest_file, "r", encoding="utf-8") as lf:
                    existing_latest_date = str(json.load(lf).get("date", "")).replace("-", "")
            except Exception:
                pass

        if not existing_latest_date or target_date_str >= existing_latest_date:
            tmp_latest_file = latest_file + ".tmp"
            with open(tmp_latest_file, "w", encoding="utf-8") as f:
                json.dump(snapshot_payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(latest_file):
                try:
                    os.remove(latest_file)
                except Exception:
                    pass
            os.replace(tmp_latest_file, latest_file)

        elapsed = time.time() - t0
        print(f"[Success] Snapshot saved successfully to:")
        print(f"  - {date_file}")
        print(f"  - {latest_file}")
        print(f"  - Total stocks: {len(stocks_data)}, Time elapsed: {elapsed:.2f}s")

        cleanup_old_snapshots(keep_count=MAX_HISTORICAL_DAYS)
        return date_file

def cleanup_old_snapshots(keep_count: int = MAX_HISTORICAL_DAYS):
    """최근 5영업일 스냅샷 파일만 유지하고, 초과된 오래된 스냅샷 파일은 자동 삭제합니다."""
    snapshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    if not os.path.exists(snapshots_dir):
        return

    pattern = re.compile(r"^snapshot_(\d{8})_1530\.json$")
    files = []
    for fname in os.listdir(snapshots_dir):
        m = pattern.match(fname)
        if m:
            files.append((m.group(1), os.path.join(snapshots_dir, fname), fname))

    files.sort(key=lambda x: x[0], reverse=True)
    if len(files) > keep_count:
        for d_str, fpath, fname in files[keep_count:]:
            try:
                os.remove(fpath)
                print(f"[*] Cleaned up old snapshot beyond 5 trading days: {fname}")
            except Exception as e:
                print(f"[Warning] Failed to remove {fname}: {e}")

if __name__ == "__main__":
    target_date = None
    force_run = False
    for arg in sys.argv[1:]:
        if arg.startswith("--date="):
            target_date = arg.split("=")[1].replace("-", "")
        elif arg == "--force":
            force_run = True
    collect_snapshot(target_date_str=target_date, force=force_run)
