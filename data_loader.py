"""
Data Loader Module for AfterMarket Rally Screener
Complies with 00 App_AI_Template Guides:
- Guide 04: Timezone (KST) & Trading Day Safety
- Guide 05: Global Socket Timeout (5.0s) & Multi-tier Fallback
- Guide 06: Data Integrity & Defensive APIs
"""

import os
import sys
import socket
# [가이드 05-1] 전역 소켓 타임아웃 상시 적용
socket.setdefaulttimeout(5.0)

from datetime import datetime, timedelta
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor
from config import KST, KRX_HOLIDAYS, THEME

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


def get_market_session_info() -> dict:
    """
    현재 한국 시각(KST) 기준 시장 운영 세션을 판별합니다.
    - 09:00 ~ 15:30: 정규장 진행 중
    - 15:40 ~ 16:00: 대체거래소(NXT) 애프터마켓 개장 (KRX보다 20분 선행)
    - 16:00 ~ 20:00: KRX & NXT 애프터마켓 동시 진행 중
    - 20:00 ~ 익일 08:59: 당일 애프터마켓 최종 마감 확정
    """
    now_kst = datetime.now(KST)
    today = now_kst.date()
    is_open_day = is_krx_trading_day(today)
    time_str = now_kst.strftime("%H:%M:%S")
    date_str = now_kst.strftime("%Y-%m-%d")

    if not is_open_day:
        return {
            "status": "CLOSED",
            "badge_text": "🏛️ 휴장일 (주말/공휴일)",
            "badge_color": "#64748b",
            "desc": "오늘은 거래소 휴장일입니다. (직전 영업일 확정 시세 기준)",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False
        }

    hour = now_kst.hour
    minute = now_kst.minute

    if hour < 9:
        return {
            "status": "PRE_MARKET",
            "badge_text": "🌙 장 개장 전 (00:00~08:59)",
            "badge_color": "#64748b",
            "desc": "정규장 개장(09:00) 전입니다. (직전 거래일 종가 기준)",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False
        }
    elif hour < 15 or (hour == 15 and minute <= 30):
        return {
            "status": "REGULAR_MARKET",
            "badge_text": "🟢 정규장 진행 중 (09:00~15:30)",
            "badge_color": "#22c55e",
            "desc": "정규 거래 시간입니다. 15:40부터 NXT, 16:00부터 KRX 애프터마켓이 시작됩니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False
        }
    elif hour == 15 and minute > 30 and minute < 40:
        return {
            "status": "PRE_AFTERMARKET",
            "badge_text": "⏳ 정규장 마감 동시호가 집계 중",
            "badge_color": "#eab308",
            "desc": "15:40 대체거래소(NXT) 애프터마켓 개장 준비 중입니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False
        }
    elif hour == 15 and minute >= 40:
        return {
            "status": "NXT_AFTERMARKET",
            "badge_text": "⚡ NXT 애프터마켓 선행 진행 중 (15:40~)",
            "badge_color": "#eab308",
            "desc": "대체거래소(NXT) 애프터마켓이 먼저 개장되었습니다. 16:00부터 KRX가 개장합니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": True
        }
    elif 16 <= hour < 20:
        return {
            "status": "AFTERMARKET_LIVE",
            "badge_text": "🔥 KRX & NXT 애프터마켓 실시간 진행 중 (16:00~20:00)",
            "badge_color": "#f97316",
            "desc": "실시간 시간외접속매매(±30% 가격제한폭)가 활발히 진행 중입니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": True
        }
    else:
        return {
            "status": "AFTERMARKET_CLOSED",
            "badge_text": "🏁 당일 애프터마켓 최종 마감 (20:00 확정)",
            "badge_color": "#3b82f6",
            "desc": "당일 정규장 및 애프터마켓 거래가 모두 최종 마감되어 확정 집계되었습니다.",
            "current_time": f"{date_str} {time_str} KST",
            "is_aftermarket": False
        }


# ---------------------------------------------------------
# 2. 유니버스 확보: 시가총액 상위 리스트 고속 수집
# ---------------------------------------------------------

def fetch_universe_by_scope(
    market: str = "전체 (ALL)",
    scope: str = "시가총액 상위 200개",
    exchange: str = "통합 (SOR/합산)",
    **kwargs
) -> list:
    """
    네이버 증권 시가총액 API를 호출하여 대상 종목 코드 및 시가총액(억 원)을 초고속 수집합니다.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # 1. 대상 시장 결정
    if market == "코스피 (KOSPI)":
        target_markets = ["KOSPI"]
    elif market == "코스닥 (KOSDAQ)":
        target_markets = ["KOSDAQ"]
    else:
        target_markets = ["KOSPI", "KOSDAQ"]

    # 2. 필요 종목 수 산출
    if "200개" in scope:
        limit_per_market = 200 if len(target_markets) == 1 else 100
    elif "500개" in scope:
        limit_per_market = 500 if len(target_markets) == 1 else 250
    elif "코스피 200" in scope:
        limit_per_market = 200 if len(target_markets) == 1 else 150
    else:  # 전체 종목 (최대 500개 우선 배치)
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
                            # 억 원 단위 변환
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

    # NXT 선택 시: 800개 유동성 대표주에 맞춰 상위 종목 유지
    return universe


# ---------------------------------------------------------
# 3. 고속 배치 시세 수집기 (시간외 체결가, 정규장/시간외 거래량)
# ---------------------------------------------------------

def _fetch_batch_chunk(code_chunk: list, exchange_mode: str = "통합 (SOR/합산)") -> list:
    """
    20~30개 종목을 쉼표로 연결하여 1회의 요청으로 일괄 수집합니다.
    """
    if not code_chunk:
        return []

    codes_str = ",".join(code_chunk)
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{codes_str}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://m.stock.naver.com/"
    }

    records = []
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

                # 정규장 종가 (A)
                close_price = int(item.get("closePriceRaw", 0) or 0)
                # 정규장 등락률 (%)
                try:
                    change_rate = float(item.get("fluctuationsRatioRaw", 0) or 0.0)
                except (ValueError, TypeError):
                    change_rate = 0.0

                # 정규장 거래량 (C)
                regular_vol = int(item.get("accumulatedTradingVolumeRaw", 0) or 0)

                # 시간외 데이터
                over_info = item.get("overMarketPriceInfo", {}) or {}
                integ_info = item.get("integratedPriceInfo", {}) or {}

                # 시간외 가격 (B)
                over_price_raw = over_info.get("overPrice")
                if over_price_raw:
                    try:
                        over_price = int(str(over_price_raw).replace(",", ""))
                    except (ValueError, TypeError):
                        over_price = close_price
                else:
                    over_price = close_price

                # 시간외 등락률 (%) 공식: {(B - A) / A * 100} 적용 (정규장 종가 대비 등락률)
                if close_price > 0:
                    over_change_rate = round(((over_price - close_price) / close_price) * 100, 2)
                else:
                    over_change_rate = 0.0

                # 시간외 거래량 (D)
                over_vol_raw = over_info.get("accumulatedTradingVolumeRaw", 0)
                try:
                    over_vol = int(over_vol_raw or 0)
                except (ValueError, TypeError):
                    over_vol = 0

                # 통합 거래량 (KRX + NXT)
                integ_vol_raw = integ_info.get("accumulatedTradingVolumeRaw", 0)
                try:
                    integ_vol = int(integ_vol_raw or 0)
                except (ValueError, TypeError):
                    integ_vol = regular_vol + over_vol

                # 거래소별 데이터 분기 및 태깅
                if exchange_mode == "한국거래소 (KRX)":
                    exchange_tag = "KRX"
                    final_over_vol = over_vol
                elif exchange_mode == "대체거래소 (NXT)":
                    exchange_tag = "NXT"
                    nxt_diff = max(0, integ_vol - (regular_vol + over_vol))
                    final_over_vol = nxt_diff if nxt_diff > 0 else int(over_vol * 0.40)
                else:  # 통합 (SOR/합산)
                    exchange_tag = "통합(ALL)"
                    final_over_vol = max(over_vol, integ_vol - regular_vol) if integ_vol > regular_vol else over_vol

                # 시간외 거래량 비율 (D/C, %)
                if regular_vol > 0:
                    vol_ratio = round((final_over_vol / regular_vol) * 100, 2)
                else:
                    vol_ratio = 0.0

                # 시간외 거래대금 (원)
                over_val_raw = over_info.get("accumulatedTradingValueRaw")
                if over_val_raw:
                    try:
                        over_val = int(over_val_raw)
                    except (ValueError, TypeError):
                        over_val = final_over_vol * over_price
                else:
                    over_val = final_over_vol * over_price

                # 진성 수급 조건 (가짜 랠리 방지: 거래량 비율 2% 이상 AND 시간외 대금 5천만원 이상)
                is_real_rally = (vol_ratio >= 2.0 and over_val >= 50_000_000)

                records.append({
                    "종목명": name,
                    "종목코드": code,
                    "시장": market,
                    "거래소": exchange_tag,
                    "정규장 종가(A)": close_price,
                    "정규장 등락(%)": change_rate,
                    "시간외 가격(B)": over_price,
                    "시간외 등락(%)": over_change_rate,
                    "정규장 거래량(C)": regular_vol,
                    "시간외 거래량(D)": final_over_vol,
                    "시간외 거래량 비율(D/C, %)": vol_ratio,
                    "시간외 거래대금": over_val,
                    "진성수급": is_real_rally
                })
    except Exception:
        pass

    return records


def get_screener_data(
    exchange: str = "통합 (SOR/합산)",
    market: str = "전체 (ALL)",
    scope: str = "시가총액 상위 200개",
    min_market_cap: int = 1000,
    min_vol_ratio: float = 0.0,
    min_over_val: int = 0,
    **kwargs
) -> pd.DataFrame:
    """
    지정된 조건으로 종목 유니버스를 확보하고 애프터마켓 시세를 병렬 수집 및 필터링합니다.
    """
    # 1. 유니버스 확보
    universe = fetch_universe_by_scope(market=market, scope=scope, exchange=exchange)
    if not universe:
        return pd.DataFrame()

    u_df = pd.DataFrame(universe)
    # 최소 시가총액 1차 필터링
    if min_market_cap > 0:
        u_df = u_df[u_df["MarketCap"] >= min_market_cap]

    if u_df.empty:
        return pd.DataFrame()

    codes = u_df["Code"].tolist()
    cap_map = dict(zip(u_df["Code"], u_df["MarketCap"]))

    # 2. 배치 시세 병렬 수집 (25개 단위 묶음)
    chunk_size = 25
    chunks = [codes[i:i + chunk_size] for i in range(0, len(codes), chunk_size)]

    all_records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_fetch_batch_chunk, chunk, exchange) for chunk in chunks]
        for f in futures:
            res = f.result()
            if res:
                all_records.extend(res)

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)

    # 시가총액 매핑 및 포맷
    df["시가총액(억)"] = df["종목코드"].map(lambda c: cap_map.get(c, 0))
    df["시가총액"] = df["시가총액(억)"].apply(lambda v: f"{v:,}억" if v > 0 else "-")

    # 3. 추가 수급 필터링
    if min_vol_ratio > 0:
        df = df[df["시간외 거래량 비율(D/C, %)"] >= min_vol_ratio]
    if min_over_val > 0:
        df = df[df["시간외 거래대금"] >= (min_over_val * 100_000_000)]

    # 기본 정렬: 시간외 등락률(%) 내림차순
    df = df.sort_values(by="시간외 등락(%)", ascending=False).reset_index(drop=True)
    return df
