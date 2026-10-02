"""
================================================================================
🏛️ 한국 증시 애프터마켓(KRX & NXT) 급변 종목 스크리너
- 정규장 종가(15:30) vs 시간외 애프터마켓(16:00~20:00) 가격·거래량 비교 분석 앱
- 00 App_AI_Template 7대 가이드 완벽 준수
================================================================================
"""

import os
import sys
import socket
# [가이드 05-1] 전역 소켓 타임아웃 5초 상시 적용
socket.setdefaulttimeout(5.0)

from datetime import datetime
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

# [가이드 05-6] 보조 모듈 강제 리로드 의무화
import importlib
import config
import data_loader
import excel_exporter
importlib.reload(config)
importlib.reload(data_loader)
importlib.reload(excel_exporter)

from config import (
    KST, EXCHANGES, DEFAULT_EXCHANGE, MARKETS, DEFAULT_MARKET,
    SCOPES, DEFAULT_SCOPE, MIN_MARKET_CAP_OPTIONS, DEFAULT_MIN_MARKET_CAP,
    EXCEL_HEADERS, THEME
)
from data_loader import get_screener_data, get_market_session_info
from excel_exporter import export_to_excel_bytes

# ==============================================================================
# 1. 페이지 기본 설정 (favicon.png 우선 적용)
# ==============================================================================
FAVICON_PATH = os.path.join(os.path.dirname(__file__), "favicon.png")
favicon = FAVICON_PATH if os.path.exists(FAVICON_PATH) else None

st.set_page_config(
    page_title="한국 증시 애프터마켓 급변 종목 스크리너",
    page_icon=favicon,
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==============================================================================
# 2. 공통 CSS 스타일 주입 (가이드 01, 02 표준 규격)
# ==============================================================================
st.markdown(
    """
    <style>
    /* [가이드 01-3] 상단 고정 헤더 배경 투명화 및 패딩 최적화 */
    header[data-testid="stHeader"] {
        background: transparent !important;
    }
    .main .block-container,
    [data-testid="stMainBlockContainer"] {
        padding-top: 1.8rem !important;
        padding-bottom: 3.5rem !important;
        max-width: 100% !important;
    }

    /* [가이드 02-1] 사이드바 다크 네이비 테마 & 텍스트 White 강제 */
    section[data-testid="stSidebar"], [data-testid="stSidebar"] {
        background-color: #1e293b !important;
        border-right: 1px solid #334155 !important;
    }
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {
        color: #f8fafc !important;
        -webkit-text-fill-color: #f8fafc !important;
    }

    /* [가이드 01-4-(2)] 사이드바 접기/열기 버튼 시인성 보강 */
    [data-testid="stSidebarCollapseButton"] button,
    [data-testid="stSidebarCollapsedControl"] button {
        background-color: #1e293b !important;
        border: 1.5px solid #38bdf8 !important;
        border-radius: 8px !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4), 0 0 6px rgba(56, 189, 248, 0.2) !important;
    }
    [data-testid="stSidebarCollapseButton"] button *,
    [data-testid="stSidebarCollapsedControl"] button * {
        color: #38bdf8 !important;
        fill: #38bdf8 !important;
    }

    /* [가이드 01-4-(3)] 파일 다운로드 버튼 통일 규격 (Slate-700 다크 네이비) */
    div[data-testid="stDownloadButton"] > button,
    .stDownloadButton > button {
        background-color: #334155 !important;
        color: #f8fafc !important;
        border: 1px solid #475569 !important;
        border-radius: 6px !important;
        font-size: 0.90rem !important;
        font-weight: 600 !important;
        height: 42px !important;
        padding: 0 20px !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        transition: all 0.2s ease-in-out !important;
    }
    div[data-testid="stDownloadButton"] > button:hover,
    .stDownloadButton > button:hover {
        background-color: #475569 !important;
        border-color: #38bdf8 !important;
        color: #ffffff !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.25) !important;
    }

    /* 메트릭 카드 다크 컨테이너 */
    .metric-card {
        background-color: #1e293b;
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 14px 16px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    .metric-title {
        color: #94a3b8;
        font-size: 0.82rem;
        font-weight: 500;
        margin-bottom: 4px;
    }
    .metric-value {
        color: #f8fafc;
        font-size: 1.45rem;
        font-weight: 700;
    }
    .metric-sub {
        font-size: 0.78rem;
        margin-top: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# ==============================================================================
# 3. 사이드바 "스크리닝 조건 설정" 패널 (가이드 02 표준 준수)
# ==============================================================================
with st.sidebar:
    # [1단계] 최상단 표준 헤더 카드
    st.markdown(
        """
        <div style='padding: 2px 0 12px 0;'>
            <div style='font-size: 1.25rem; font-weight: 700; color: #f8fafc; letter-spacing: -0.01em; display: flex; align-items: center; gap: 8px;'>
                <span>⚙️</span> 시장/필터 설정
            </div>
            <div style='font-size: 0.82rem; color: #94a3b8; margin-top: 4px; line-height: 1.4;'>
                애프터마켓 거래량 급변 및 가격 왜곡 종목을 정밀 발굴합니다.
            </div>
        </div>
        <hr style='border: 0; height: 1px; background-color: #334155; margin: 10px 0 16px 0;'>
        """,
        unsafe_allow_html=True
    )

    # [2단계] 거래소 선택 (기본값: 통합(SOR/합산))
    st.markdown("<div style='font-size: 0.95rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px;'>🏛️ 거래소 선택</div>", unsafe_allow_html=True)
    sel_exchange = st.radio(
        "거래소 선택",
        EXCHANGES,
        index=0,  # 기본값: "통합 (SOR/합산)"
        horizontal=True,
        label_visibility="collapsed"
    )

    st.markdown("<hr style='border: 0; height: 1px; background-color: #334155; margin: 16px 0;'>", unsafe_allow_html=True)

    # [3단계] 시장 선택
    st.markdown("<div style='font-size: 0.95rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px;'>🏢 시장 선택</div>", unsafe_allow_html=True)
    sel_market = st.radio(
        "시장 선택",
        MARKETS,
        index=0,  # 기본값: 전체 (ALL)
        horizontal=True,
        label_visibility="collapsed"
    )

    st.markdown("<hr style='border: 0; height: 1px; background-color: #334155; margin: 16px 0;'>", unsafe_allow_html=True)

    # [4단계] 대상 범위 (Scope)
    st.markdown("<div style='font-size: 0.95rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px;'>🎯 대상 범위 (Scope)</div>", unsafe_allow_html=True)
    sel_scope = st.selectbox(
        "대상 범위",
        SCOPES,
        index=0,  # 기본값: 시가총액 상위 200개 (쾌속 모드)
        label_visibility="collapsed"
    )

    st.markdown("<hr style='border: 0; height: 1px; background-color: #334155; margin: 16px 0;'>", unsafe_allow_html=True)

    # [5단계] 최소 시가총액
    st.markdown("<div style='font-size: 0.95rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px;'>💰 최소 시가총액 (억 원)</div>", unsafe_allow_html=True)
    sel_min_market_cap = st.select_slider(
        "최소 시가총액",
        options=MIN_MARKET_CAP_OPTIONS,
        value=DEFAULT_MIN_MARKET_CAP,
        format_func=lambda x: f"{x:,}억 원" if x > 0 else "제한 없음 (전체)",
        label_visibility="collapsed"
    )

    # [6단계] 보조 수급 필터링 (Expander)
    with st.expander("🛠️ 스마트 수급 필터 설정", expanded=False):
        sel_min_vol_ratio = st.slider(
            "최소 시간외 거래량 비율 (D/C, %)",
            min_value=0.0,
            max_value=10.0,
            value=0.0,
            step=0.5,
            format="%.1f%%",
            help="정규장 거래량(C) 대비 시간외 거래량(D)의 최소 비율입니다."
        )
        sel_min_val = st.slider(
            "최소 시간외 거래대금 (억 원)",
            min_value=0,
            max_value=20,
            value=0,
            step=1,
            format="%d억",
            help="소량(10~100주) 허수 매매를 차단하기 위한 거래대금 기준입니다."
        )
        only_real_rally = st.checkbox("🔥 진성 수급주만 보기 (D/C ≥ 2% & 대금 5천만+)", value=True)

    # [7단계] 하단 액션 버튼 (가이드 02 Type A 표준: 2열 가로 배치)
    st.markdown("<hr style='border: 0; height: 1px; background-color: #334155; margin: 20px 0 16px 0;'>", unsafe_allow_html=True)
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        btn_update = st.button("🔄 Update", use_container_width=True, help="캐시를 초기화하고 최신 데이터를 다시 수집합니다.")
        if btn_update:
            st.cache_data.clear()
            st.rerun()
    with col_b2:
        btn_search = st.button("🔍 조회", type="primary", use_container_width=True, help="선택한 조건으로 화면을 갱신합니다.")
        if btn_search:
            st.rerun()

# ==============================================================================
# 4. 메인 타이틀 및 시장 상태 바 (가이드 01-3, 가이드 04 표준)
# ==============================================================================
session_info = get_market_session_info()

st.markdown(
    """
    <div style='text-align: center; padding-top: 0.5rem;'>
        <h1 style='color: #8AB4F8; font-size: 2.0rem; font-weight: 800; margin-bottom: 6px; letter-spacing: -0.02em;'>
            한국 증시 애프터마켓 급변 종목 스크리너
        </h1>
        <p style='color: #94a3b8; font-size: 0.95rem; margin-bottom: 14px;'>
            정규장 공식 종가(15:30) vs 애프터마켓(16:00~20:00) 체결가 및 거래량을 정밀 비교하여 내일의 주도주를 발굴합니다.
        </p>
    </div>
    """,
    unsafe_allow_html=True
)

# 시장 운영 세션 알림 바
st.markdown(
    f"""
    <div style='background-color: #1e293b; border-left: 4px solid {session_info["badge_color"]}; padding: 10px 16px; border-radius: 6px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;'>
        <div style='display: flex; align-items: center; gap: 10px;'>
            <span style='background-color: {session_info["badge_color"]}22; color: {session_info["badge_color"]}; font-weight: 700; font-size: 0.85rem; padding: 4px 10px; border-radius: 4px; border: 1px solid {session_info["badge_color"]}44;'>
                {session_info["badge_text"]}
            </span>
            <span style='color: #cbd5e1; font-size: 0.85rem;'>{session_info["desc"]}</span>
        </div>
        <div style='color: #94a3b8; font-size: 0.82rem;'>
            🕒 KST 기준시각: <span style='color: #f8fafc; font-weight: 600;'>{session_info["current_time"]}</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

# ==============================================================================
# 5. 데이터 수집 및 캐싱 (가이드 04-2, 05-4 준수)
# ==============================================================================
@st.cache_data(ttl=60, show_spinner=False)
def load_cached_screener(exchange, market, scope, min_cap, min_v_ratio, min_val, only_real):
    df = get_screener_data(
        exchange=exchange,
        market=market,
        scope=scope,
        min_market_cap=min_cap,
        min_vol_ratio=min_v_ratio,
        min_over_val=min_val
    )
    if only_real and not df.empty and "진성수급" in df.columns:
        df = df[df["진성수급"] == True]
    return df

with st.spinner("애프터마켓 실시간 체결 데이터를 고속 수집 및 분석 중입니다..."):
    df_raw = load_cached_screener(
        sel_exchange, sel_market, sel_scope,
        sel_min_market_cap, sel_min_vol_ratio, sel_min_val, only_real_rally
    )

if df_raw.empty:
    st.warning("⚠️ 선택하신 조건에 일치하는 종목이 없습니다. 사이드바에서 시가총액이나 수급 필터 조건을 완화해 보세요.")
    st.stop()

# ==============================================================================
# 6. 상단 핵심 KPI 요약 메트릭 카드 4종
# ==============================================================================
total_count = len(df_raw)
top_gain_row = df_raw.sort_values(by="시간외 등락(%)", ascending=False).iloc[0]
top_vol_row = df_raw.sort_values(by="시간외 거래량 비율(D/C, %)", ascending=False).iloc[0]
top_val_row = df_raw.sort_values(by="시간외 거래대금", ascending=False).iloc[0]

mc1, mc2, mc3, mc4 = st.columns(4)
with mc1:
    st.markdown(
        f"""
        <div class='metric-card'>
            <div class='metric-title'>🎯 스크리닝 포착 종목</div>
            <div class='metric-value'>{total_count:,} <span style='font-size: 0.9rem; font-weight: normal; color: #94a3b8;'>개</span></div>
            <div class='metric-sub' style='color: #38bdf8;'>거래소: {sel_exchange.split()[0]} | {sel_market.split()[0]}</div>
        </div>
        """,
        unsafe_allow_html=True
    )
with mc2:
    gain_color = THEME["up_red"] if top_gain_row["시간외 등락(%)"] > 0 else THEME["down_blue"]
    st.markdown(
        f"""
        <div class='metric-card'>
            <div class='metric-title'>🚀 시간외 최대 급등 종목</div>
            <div class='metric-value' style='color: {gain_color};'>{top_gain_row["종목명"]}</div>
            <div class='metric-sub' style='color: {gain_color};'>등락률: +{top_gain_row["시간외 등락(%)"]:.2f}% ({top_gain_row["시간외 가격(B)"]:,}원)</div>
        </div>
        """,
        unsafe_allow_html=True
    )
with mc3:
    st.markdown(
        f"""
        <div class='metric-card'>
            <div class='metric-title'>⚡ 시간외 거래량 비율 1위</div>
            <div class='metric-value' style='color: #facc15;'>{top_vol_row["종목명"]}</div>
            <div class='metric-sub' style='color: #facc15;'>정규장 대비 비중: {top_vol_row["시간외 거래량 비율(D/C, %)"]:.2f}%</div>
        </div>
        """,
        unsafe_allow_html=True
    )
with mc4:
    val_eok = int(top_val_row["시간외 거래대금"] / 100_000_000)
    st.markdown(
        f"""
        <div class='metric-card'>
            <div class='metric-title'>💰 시간외 거래대금 1위</div>
            <div class='metric-value'>{top_val_row["종목명"]}</div>
            <div class='metric-sub' style='color: #a855f7;'>거래대금: {val_eok:,}억 원 ({top_val_row["시간외 거래량(D)"]:,}주)</div>
        </div>
        """,
        unsafe_allow_html=True
    )

# ==============================================================================
# 7. 시각적 분석: 수급-가격 모멘텀 4분면 스캐터 플롯 (가이드 03 준수)
# ==============================================================================
st.markdown(
    """
    <div style='font-size: 1.20rem; font-weight: 700; color: #8AB4F8; margin: 24px 0 10px 0; display: flex; align-items: center; gap: 8px;'>
        <span>📈</span> 수급-가격 모멘텀 4분면 분석 (Scatter Matrix)
    </div>
    """,
    unsafe_allow_html=True
)

fig = px.scatter(
    df_raw.head(80),
    x="시간외 등락(%)",
    y="시간외 거래량 비율(D/C, %)",
    size="시간외 거래대금",
    color="시간외 등락(%)",
    hover_name="종목명",
    hover_data={
        "종목코드": True,
        "시장": True,
        "정규장 종가(A)": ":,",
        "시간외 가격(B)": ":,",
        "시간외 등락(%)": ":.2f%",
        "시간외 거래량 비율(D/C, %)": ":.2f%",
        "시가총액": True
    },
    color_continuous_scale=["#3b82f6", "#94a3b8", "#f87171"],
    size_max=35,
    template="plotly_dark"
)

fig.update_layout(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#f8fafc", family="sans-serif"),
    margin=dict(l=40, r=40, t=20, b=40),
    height=420,
    coloraxis_showscale=False,
    xaxis=dict(
        title="시간외 등락률 (%)",
        gridcolor="#334155",
        zerolinecolor="#64748b",
        zerolinewidth=1.5
    ),
    yaxis=dict(
        title="시간외 거래량 비율 (D/C, %)",
        gridcolor="#334155",
        zerolinecolor="#64748b",
        zerolinewidth=1.5
    )
)

# 1사분면(급등 + 고수급) 하이라이트 주석
fig.add_annotation(
    text="🔥 1사분면: 대량 수급 동반 급등주 (내일의 주도주 후보)",
    xref="paper", yref="paper",
    x=0.98, y=0.95,
    showarrow=False,
    font=dict(color="#fca5a5", size=11, family="sans-serif"),
    bgcolor="rgba(239, 68, 68, 0.15)",
    bordercolor="#ef4444",
    borderwidth=1,
    borderpad=6
)

st.plotly_chart(fig, use_container_width=True)

# ==============================================================================
# 8. 엑셀 형식 데이터 테이블 (가이드 01-6 및 오름차순/내림차순 토글 구현)
# ==============================================================================
st.markdown(
    """
    <div style='font-size: 1.20rem; font-weight: 700; color: #8AB4F8; margin: 24px 0 10px 0; display: flex; align-items: center; gap: 8px;'>
        <span>📊</span> 애프터마켓 스크리닝 데이터 (Excel Spreadsheet View)
    </div>
    """,
    unsafe_allow_html=True
)

# 정렬 컨트롤러 바 (웹 상에서 빠른 토글 정렬 지원)
col_sort1, col_sort2, col_filter, col_spacer = st.columns([2, 1.5, 2.5, 2])
with col_sort1:
    sort_column = st.selectbox(
        "정렬 기준 컬럼",
        [
            "시간외 등락(%)",
            "시간외 거래량 비율(D/C, %)",
            "정규장 등락(%)",
            "정규장 거래량(C)",
            "시간외 거래량(D)",
            "시간외 거래대금"
        ],
        index=0
    )
with col_sort2:
    sort_order = st.radio(
        "정렬 순서",
        ["내림차순 (▼)", "오름차순 (▲)"],
        horizontal=True
    )
with col_filter:
    search_keyword = st.text_input("🔍 종목명/코드 검색", placeholder="예: 삼성전자, 005930")

# 정렬 및 검색 적용
df_display = df_raw.copy()
if search_keyword:
    kw = search_keyword.strip()
    df_display = df_display[
        df_display["종목명"].str.contains(kw, case=False, na=False) |
        df_display["종목코드"].str.contains(kw, case=False, na=False)
    ]

ascending = (sort_order == "오름차순 (▲)")
df_display = df_display.sort_values(by=sort_column, ascending=ascending).reset_index(drop=True)

# 엑셀 헤더 규격에 맞춘 컬럼 정렬
display_cols = [c for c in EXCEL_HEADERS if c in df_display.columns]

# 인터랙티브 데이터 테이블 렌더링 (컬럼 헤더 클릭 시 자체 토글 정렬 지원)
st.dataframe(
    df_display[display_cols],
    use_container_width=True,
    height=480,
    column_config={
        "종목명": st.column_config.TextColumn("종목명", width="medium"),
        "종목코드": st.column_config.TextColumn("종목코드", width="small"),
        "시장": st.column_config.TextColumn("시장", width="small"),
        "거래소": st.column_config.TextColumn("거래소", width="small"),
        "시가총액": st.column_config.TextColumn("시가총액", width="medium"),
        "정규장 종가(A)": st.column_config.NumberColumn("정규장 종가(A)", format="%d원"),
        "정규장 등락(%)": st.column_config.NumberColumn("정규장 등락(%)", format="%.2f%%"),
        "시간외 가격(B)": st.column_config.NumberColumn("시간외 가격(B)", format="%d원"),
        "시간외 등락(%)": st.column_config.NumberColumn("시간외 등락(%)", format="%.2f%%"),
        "정규장 거래량(C)": st.column_config.NumberColumn("정규장 거래량(C)", format="%d주"),
        "시간외 거래량(D)": st.column_config.NumberColumn("시간외 거래량(D)", format="%d주"),
        "시간외 거래량 비율(D/C, %)": st.column_config.NumberColumn("시간외 거래량 비율(D/C, %)", format="%.2f%%")
    },
    hide_index=True
)

# ==============================================================================
# 9. 엑셀 파일 다운로드 버튼 (가이드 01-4-(3) 표준 규격 준수)
# ==============================================================================
excel_data = export_to_excel_bytes(df_display)
now_file_str = datetime.now(KST).strftime("%Y%m%d_%H%M")
excel_filename = f"AfterMarket_Rally_{sel_exchange.split()[0]}_{now_file_str}.xlsx"

st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
col_dl1, col_dl2 = st.columns([1, 3])
with col_dl1:
    st.download_button(
        label="📥 엑셀 파일 다운로드",
        data=excel_data,
        file_name=excel_filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        help="1행 헤더에 오름차순/내림차순 토글 AutoFilter가 적용된 엑셀 파일입니다."
    )
with col_dl2:
    st.caption("💡 다운로드받은 엑셀(.xlsx) 파일은 1행 헤더에 **AutoFilter 토글 역삼각형(▼)**이 기본 적용되어 있어, 엑셀에서도 각 컬럼별 정렬 및 필터링을 즉시 사용하실 수 있습니다.")

# ==============================================================================
# 10. 하단 표준 법적 고지 (가이드 01-5-(2) 표준 준수)
# ==============================================================================
st.markdown("<hr style='border: 0; height: 1px; background-color: #334155; margin: 30px 0 10px 0;'>", unsafe_allow_html=True)
st.markdown(
    """
    <div style='text-align: center; color: #64748b; font-size: 0.8rem; margin-top: 8px; margin-bottom: 24px; line-height: 1.6;'>
        ⚠️ 본 서비스에서 제공하는 모든 정보는 투자 참고용이며, 시간외 시장은 호가 유동성이 얇아 시세 왜곡 및 갭 메우기 위험이 존재합니다. 투자의 최종 결정과 책임은 투자자 본인에게 있습니다.
    </div>
    """,
    unsafe_allow_html=True
)
