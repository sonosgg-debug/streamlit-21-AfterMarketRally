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

def collect_snapshot(target_date_str: str = None, force: bool = False) -> str:
    """
    15:30 정규장 마감 및 애프터마켓 실제 체결가/수급 스냅샷을 수집하고 snapshots/ 폴더에 JSON 파일로 저장합니다.
    """
    now_kst = datetime.now(KST)

    # 수집 대상 날짜 결정 및 검증
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, "%Y%m%d").date()
        except Exception:
            target_date = now_kst.date()
    else:
        target_date = now_kst.date()
        target_date_str = now_kst.strftime("%Y%m%d")

    snapshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)
    date_file = os.path.join(snapshots_dir, f"snapshot_{target_date_str}_1530.json")
    latest_file = os.path.join(snapshots_dir, "latest_snapshot.json")

    # Early return: 이미 해당 날짜의 유효한 스냅샷이 존재하고, over_price 필드가 온전히 갖춰져 있으면 기존 파일 반환
    if not force and os.path.exists(date_file):
        try:
            with open(date_file, "r", encoding="utf-8") as f:
                d = json.load(f)
                stocks = d.get("stocks", {})
                if stocks and len(stocks) > 0:
                    sample_stock = next(iter(stocks.values()))
                    if "over_price" in sample_stock:
                        print(f"[*] Snapshot for {target_date_str} already exists with over_price ({len(stocks)} stocks). Returning existing file.")
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

            stocks_data[code] = {
                "name": name,
                "market": mkt,
                "market_cap": cap,
                "close_price": close_price,
                "over_price": over_price,
                "over_change_rate": over_change_rate,
                "krx_volume": krx_vol,
                "krx_value": krx_val,
                "nxt_volume": nxt_vol,
                "nxt_value": nxt_val,
                "total_volume": total_vol,
                "total_value": total_val
            }

        snapshot_payload = {
            "date": target_date_str,
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
