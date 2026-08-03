@echo off
title PrismClaw Agent
chcp 65001 >nul 2>&1

echo ========================================
echo   PrismClaw Agent - Context Spectrum
echo ========================================
echo.

cd /d "%~dp0"

REM ---- 激活 conda 环境（否则 python 可能被 WindowsApps 空壳劫持）----
call C:\ProgramData\miniconda3\Scripts\activate.bat ai-course >nul 2>&1
if errorlevel 1 (
    echo [!] conda not found, trying system python...
)
set "PYTHON=python"

REM ---- 清理上一轮残留：按端口杀旧进程 + 清 __pycache__ ----
echo [*] Cleaning up any previous instance...
%PYTHON% cleanup.py
echo.

echo Starting server on port 8765...
echo Opening browser when ready...
echo.

REM ---- 启动新 server（窗口标题便于识别）----
start "PrismClaw Server" /B %PYTHON% server.py

REM ---- 等端口真正起来再开浏览器 ----
set "READY=0"
for /L %%i in (1,1,20) do (
    netstat -ano 2>nul | findstr ":8765 " | findstr "LISTENING" >nul 2>&1
    if not errorlevel 1 (
        set "READY=1"
        goto :OPEN
    )
    timeout /t 1 /nobreak >nul
)
:OPEN
if "%READY%"=="1" (
    echo [OK] Server is up. Opening browser...
    start http://localhost:8765
) else (
    echo [!] Server did not come up in time. Check server.py / port 8765.
    echo     Open http://localhost:8765 manually after it starts.
)

echo.
echo Press any key to stop server and exit...
pause >nul

REM ---- 退出时也清干净（再杀一次端口占用）----
echo [*] Shutting down...
%PYTHON% cleanup.py
echo Bye.
