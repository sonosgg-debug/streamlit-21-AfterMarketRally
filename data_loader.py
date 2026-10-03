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

    return universe


# ---------------------------------------------------------
# 3. 네이버 증권 & 다음 금융 상호보완적 시세 수집기
# ---------------------------------------------------------

def _fetch_naver_batch(code_chunk: list) -> dict:
    """
    네이버 증권 실시간 폴링 API: KRX 누적 거래량(C), NXT 누적 거래량(D) 고속 배치 수집
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

                # KRX 누적 거래량 (C)
                c_vol = int(item.get("accumulatedTradingVolumeRaw", 0) or 0)

                # NXT 누적 거래량 (D)
                over_info = item.get("overMarketPriceInfo", {}) or {}
                d_vol = int(over_info.get("accumulatedTradingVolumeRaw", 0) or 0)

                # 네이버 종가 (Fallback용)
                naver_close = int(item.get("closePriceRaw", 0) or 0)

                # 대체거래소(NXT) 거래대금
                nxt_val_raw = over_info.get("accumulatedTradingValueRaw")
                try:
                    nxt_val = int(nxt_val_raw or 0)
                except (ValueError, TypeError):
                    nxt_val = 0

                result[code] = {
                    "name": name,
                    "market": market,
                    "krx_vol": c_vol,
                    "nxt_vol": d_vol,
                    "naver_close": naver_close,
                    "nxt_val": nxt_val
                }
    except Exception:
        pass

    return result


def _fetch_daum_quote(code: str) -> tuple:
    """
    다음(Daum) 금융 API: KRX 정규장 공식 종가(regularTradePrice) 및 시간외 현재가(tradePrice) 정밀 수집
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


def get_screener_data(
    market: str = "전체 (ALL)",
    scope: str = "시가총액 상위 200개",
    min_market_cap: int = 1000,
    min_nxt_ratio: float = 0.0,
    min_over_val: int = 0,
    **kwargs
) -> pd.DataFrame:
    """
    지정된 조건으로 종목 유니버스를 확보하고 네이버 증권 및 다음 금융의 데이터를 상호 보완하여 수집 및 분석합니다.
    """
    # 1. 유니버스 확보
    universe = fetch_universe_by_scope(market=market, scope=scope)
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

    # 2. 네이버 배치 시세 병렬 수집 (거래량 C, D 확보)
    chunk_size = 25
    chunks = [codes[i:i + chunk_size] for i in range(0, len(codes), chunk_size)]

    naver_data = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_fetch_naver_batch, chunk) for chunk in chunks]
        for f in futures:
            res = f.result()
            if res:
                naver_data.update(res)

    # 3. 다음 금융 병렬 수집 (KRX 정규장 종가 & 시간외 가격 확보)
    daum_data = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        daum_results = list(executor.map(_fetch_daum_quote, codes))
        for code, reg_p, trade_p in daum_results:
            daum_data[code] = (reg_p, trade_p)

    # 4. 데이터 통합 및 상호 보완 가공
    records = []
    for code in codes:
        n_info = naver_data.get(code, {})
        d_info = daum_data.get(code, (None, None))

        name = n_info.get("name", "")
        mkt = n_info.get("market", "")
        cap = cap_map.get(code, 0)
        c_vol = n_info.get("krx_vol", 0)
        d_vol = n_info.get("nxt_vol", 0)
        naver_close = n_info.get("naver_close", 0)
        nxt_val = n_info.get("nxt_val", 0)

        # 다음 금융에서 정규장 종가와 시간외 가격 추출 (부재 시 네이버 Fallback)
        daum_reg, daum_trade = d_info
        reg_price = int(daum_reg) if daum_reg else naver_close
        over_price = int(daum_trade) if daum_trade else naver_close

        # 시간외 등락률(%) = {(시간외 가격 - 정규장 종가) / 정규장 종가} * 100
        if reg_price > 0 and over_price > 0:
            over_change_rate = round(((over_price - reg_price) / reg_price) * 100, 2)
        else:
            over_change_rate = 0.0

        # NXT 비중(%) = {D / (C + D)} * 100
        total_vol = c_vol + d_vol
        nxt_ratio = round((d_vol / total_vol) * 100, 2) if total_vol > 0 else 0.0

        # NXT 거래대금 보정
        if nxt_val == 0 and d_vol > 0 and over_price > 0:
            nxt_val = d_vol * over_price

        # 진성 수급 조건 (NXT 비중 30% 이상 & NXT 거래대금 10억 원 이상)
        is_real_rally = (nxt_ratio >= 30.0 and nxt_val >= 1_000_000_000)

        records.append({
            "종목명": name,
            "종목코드": code,
            "시장": mkt,
            "시가총액(억)": cap,
            "시가총액": f"{cap:,}억" if cap > 0 else "-",
            "KRX 정규장 종가": reg_price,
            "KRX 시간외 가격": over_price,
            "시간외 등락률(%)": over_change_rate,
            "KRX 거래량": c_vol,
            "NXT 거래량": d_vol,
            "NXT 비중(%)": nxt_ratio,
            "NXT 거래대금": nxt_val,
            "진성수급": is_real_rally
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # 5. 추가 수급 필터링
    if min_nxt_ratio > 0:
        df = df[df["NXT 비중(%)"] >= min_nxt_ratio]
    if min_over_val > 0:
        df = df[df["NXT 거래대금"] >= (min_over_val * 100_000_000)]

    # 기본 정렬: 시간외 등락률(%) 내림차순
    df = df.sort_values(by="시간외 등락률(%)", ascending=False).reset_index(drop=True)
    return df
