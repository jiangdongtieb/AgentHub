@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   AgentHub 启动中...
echo.
where python >nul 2>nul
if %errorlevel%==0 (
    python server.py
) else (
    "C:\Users\35632\AppData\Local\Programs\Python\Python313\python.exe" server.py
)
pause
