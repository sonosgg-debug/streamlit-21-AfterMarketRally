@echo off
chcp 65001 > nul
echo ============================================================
echo   04_AfterMarketRally Deploy to Oracle Server
echo ============================================================
python "%~dp0deploy_oracle.py"
pause
