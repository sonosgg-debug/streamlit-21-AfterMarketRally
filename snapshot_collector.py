"""
================================================================================
🏛️ 15:30 정규장 마감 스냅샷 수집기 (Market Close Snapshot Collector)
- 실행 시점: 월~금 매일 한국 시각 15:35 (UTC 06:35)
- 기능: 시가총액 상위 종목(600개)의 정규장 마감 거래량/거래대금/종가를 즉시 수집하여 스냅샷 JSON 파일로 저장
- 저장 경로: snapshots/snapshot_YYYYMMDD_1530.json 및 snapshots/latest_snapshot.json
================================================================================
"""

import os
import sys
import json
import time

from datetime import datetime, timezone, timedelta
import requests
from concurrent.futures import ThreadPoolExecutor

# 타임존 설정 (KST = UTC+9)
KST = timezone(timedelta(hours=9))

# KRX 법정 공휴일 캘린더 (2025~2027)
KRX_HOLIDAYS = {
    # 2025년
    "20250101", "20250127", "20250128", "20250129", "20250130",
    "20250303", "20250505", "20250506", "20250606", "20250815",
    "20251003", "20251006", "20251007", "20251008", "20251009",
    "20251225", "20251231",
    # 2026년
    "20260101", "20260216", "20260217", "20260218", "20260302",
    "20260505", "20260525", "20260603", "20260717", "20260817",
    "20260924", "20260925", "20261005", "20261009", "20261225", "20261231",
    # 2027년
    "20270101", "20270205", "20270208", "20270209", "20270301",
    "20270505", "20270513", "20270604", "20270816", "20270914",
    "20270915", "20270916", "20271004", "20271011", "20271225", "20271231"
}

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
    15:30 정규장 마감 스냅샷을 수집하고 snapshots/ 폴더에 JSON 파일로 저장합니다.
    """
    now_kst = datetime.now(KST)

    # 거래일 검증
    if not force:
        today = now_kst.date()
        if not is_krx_trading_day(today):
            print(f"[*] Today ({today}) is a weekend or holiday. Snapshot skipped.")
            return None

    if target_date_str is None:
        target_date_str = now_kst.strftime("%Y%m%d")

    print(f"[*] Starting 15:30 Snapshot collection for date: {target_date_str} at {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}...")
    t0 = time.time()

    # 1. 대상 유니버스 수집 (코스피 300 + 코스닥 300 = 600개)
    universe = fetch_universe(limit_per_market=300)
    if not universe:
        print("[Error] Failed to collect stock universe.")
        return None
    print(f"[*] Universe collected: {len(universe)} stocks.")

    codes = [u["code"] for u in universe]
    code_map = {u["code"]: u for u in universe}

    # 2. 배치 시세 수집
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

        # NXT 누적 거래량 & 거래대금
        over_info = item.get("overMarketPriceInfo", {}) or {}
        nxt_vol = int(over_info.get("accumulatedTradingVolumeRaw", 0) or 0)
        nxt_val = int(over_info.get("accumulatedTradingValueRaw", 0) or 0)

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

    # 4. 파일 저장
    snapshots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(snapshots_dir, exist_ok=True)

    date_file = os.path.join(snapshots_dir, f"snapshot_{target_date_str}_1530.json")
    latest_file = os.path.join(snapshots_dir, "latest_snapshot.json")

    with open(date_file, "w", encoding="utf-8") as f:
        json.dump(snapshot_payload, f, ensure_ascii=False, indent=2)

    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(snapshot_payload, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print(f"[Success] Snapshot saved successfully to:")
    print(f"  - {date_file}")
    print(f"  - {latest_file}")
    print(f"  - Total stocks: {len(stocks_data)}, Time elapsed: {elapsed:.2f}s")
    return date_file

if __name__ == "__main__":
    # CLI 파라미터 처리
    target_date = None
    force_run = False

    for arg in sys.argv[1:]:
        if arg.startswith("--date="):
            target_date = arg.split("=")[1].replace("-", "")
        elif arg == "--force":
            force_run = True

    # GitHub Actions 환경이거나 수동 실행 시
    if "--force" not in sys.argv:
        force_run = True  # 직접 실행 시 기본 허용

    collect_snapshot(target_date_str=target_date, force=force_run)
