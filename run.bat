@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title MyDM-Lite Downloader

rem ===================================================================
rem  MyDM-Lite launcher.
rem  IMPORTANT: keep this file ASCII-only. cmd.exe mis-parses UTF-8
rem  comments on Chinese Windows, so every Chinese message lives in
rem  _launcher.py and is shown as a message box from there.
rem ===================================================================

rem ---------- 1) Detect Python (prefer the py launcher) ----------
set "PY_CMD="
py -3 --version >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    python --version >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [MyDM-Lite] Python not found.
    echo [MyDM-Lite] Please install Python 3.9 or above from python.org
    echo.
    pause
    exit /b 1
)

rem ---------- 2) Check Python version is 3.9 or newer ----------
%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 (
    %PY_CMD% _launcher.py too-old
    pause
    exit /b 1
)

rem ---------- 3) Check dependencies: only go online when really missing ----------
%PY_CMD% -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo [MyDM-Lite] First run: installing dependencies, please wait...
    %PY_CMD% -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        %PY_CMD% _launcher.py no-deps
        pause
        exit /b 1
    )
    %PY_CMD% -c "import requests" >nul 2>nul
    if errorlevel 1 (
        %PY_CMD% _launcher.py no-deps
        pause
        exit /b 1
    )
)

rem ---------- 4) Start the app ----------
rem MYDM_SELFTEST=1 makes the window close itself after 3s (smoke test only)
set "MYDM_ACTION=start"
if defined MYDM_SELFTEST set "MYDM_ACTION=selftest"

%PY_CMD% _launcher.py %MYDM_ACTION%
if errorlevel 1 (
    echo [MyDM-Lite] Program exited with an error. See mydm.log for details.
    pause
    exit /b 1
)

endlocal
exit /b 0
