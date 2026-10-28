@echo off
chcp 65001 >nul
title CLI 登录助手

REM ── 关键：把环境变量纠正为真实用户目录 ──
REM 若从 Loomy 里启动，%USERPROFILE%/%LOCALAPPDATA% 会被指向 Loomy 目录，
REM 导致 CLI 把凭据写到错误的档案里。这里统一改回本机真实用户目录。
for %%I in ("%~dp0..\..\..") do set "REALHOME=%%~fI"
set "USERPROFILE=%REALHOME%"
set "HOME=%REALHOME%"
set "APPDATA=%REALHOME%\AppData\Roaming"
set "LOCALAPPDATA=%REALHOME%\AppData\Local"
set "TEMP=%REALHOME%\AppData\Local\Temp"
set "TMP=%TEMP%"
set "PATH=%REALHOME%\.local;%REALHOME%\.local\bin;%REALHOME%\.kimi-code\bin;%REALHOME%\AppData\Local\trae-cli\bin;%REALHOME%\AppData\Local\codebuddy\bin;%PATH%"

echo.
echo   真实用户目录: %REALHOME%
echo   即将打开登录窗口，浏览器里的最后一步（扫码 / 手机号）请你完成。
echo.
echo   [1] CodeBuddy    [2] Trae    [3] Kimi    [4] 全部打开    [0] 退出
echo.
set /p CH=请输入编号:

if "%CH%"=="1" start "CodeBuddy 登录" cmd /k codebuddy
if "%CH%"=="2" start "Trae 登录" cmd /k traecli
if "%CH%"=="3" start "Kimi 登录" cmd /k kimi login
if "%CH%"=="4" (
    start "CodeBuddy 登录" cmd /k codebuddy
    start "Trae 登录" cmd /k traecli
    start "Kimi 登录" cmd /k kimi login
)
echo.
echo   提示：qwen 不支持网页登录，需要在终端配置 API Key，例如：
echo     qwen --auth-type openai --openai-api-key ^<你的key^>
echo.
pause
