@echo off
chcp 65001 > nul
cd /d "%~dp0"
title AfterMarket Rally Screener

echo ===================================================
echo   KRX and NXT AfterMarket Rally Screener
echo ===================================================
echo.

if exist ".venv\Scripts\activate.bat" (
    echo [가상환경] .venv 활성화 중...
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    echo [가상환경] venv 활성화 중...
    call "venv\Scripts\activate.bat"
)

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [오류] Python이 설치되어 있지 않거나 PATH에 등록되지 않았습니다.
    pause
    exit /b 1
)

python -m streamlit --version >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [알림] 필수 패키지를 설치합니다...
    pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [오류] 패키지 설치 실패
        pause
        exit /b 1
    )
)

echo.
echo [실행] Streamlit 스크리너 대시보드를 시작합니다...
echo - 로컬 주소: http://localhost:8501
echo - 서버 기동 완료 후 브라우저가 자동으로 열립니다.
echo - 종료하려면 이 창에서 Ctrl+C를 누르세요.
echo.

python -m streamlit run app.py --server.headless false %*

if %ERRORLEVEL% neq 0 (
    echo.
    echo [알림] 앱 실행이 종료되었습니다.
    echo.
)

pause
