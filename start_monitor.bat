@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AgentHub 协作台监视器
echo.
echo   协作台监视器启动中...（保持此窗口开启）
echo.
where python >nul 2>nul
if %errorlevel%==0 (
    python -u monitor.py
) else (
    "C:\Users\35632\AppData\Local\Programs\Python\Python313\python.exe" -u monitor.py
)
pause
