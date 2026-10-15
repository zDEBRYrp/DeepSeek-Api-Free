@echo off
setlocal

rem Always run from the OwnApi directory, even after a double-click.
cd /d "%~dp0"

if not defined OWNAPI_HOST set "OWNAPI_HOST=127.0.0.1"
if not defined OWNAPI_PORT set "OWNAPI_PORT=8000"
set "OWNAPI_PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%OWNAPI_PYTHON%" (
    echo [ERROR] Python environment was not found:
    echo         %OWNAPI_PYTHON%
    echo.
    echo Create it first:
    echo   py -3.12 -m venv .venv
    echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

"%OWNAPI_PYTHON%" -c "import encodings" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python environment is damaged: standard library is missing.
    echo.
    echo Repair it with:
    echo   rmdir /s /q .venv
    echo   py -3.12 -m venv .venv
    echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:choose_browser_mode
echo Browser mode:
echo   [1] Headless - browser runs hidden ^(default^)
echo   [2] Visible  - open the browser window
set "OWNAPI_MODE="
set /p "OWNAPI_MODE=Select 1 or 2 [1]: "
if "%OWNAPI_MODE%"=="" set "OWNAPI_MODE=1"
if "%OWNAPI_MODE%"=="1" (
    set "HEADLESS=true"
    set "OWNAPI_BROWSER_LABEL=headless"
    goto browser_mode_selected
)
if "%OWNAPI_MODE%"=="2" (
    set "HEADLESS=false"
    set "OWNAPI_BROWSER_LABEL=visible"
    goto browser_mode_selected
)
echo Invalid choice. Enter 1, 2, or press Enter for headless.
echo.
goto choose_browser_mode

:browser_mode_selected
echo.
echo ============================================================
echo  OwnApi
echo  Browser: %OWNAPI_BROWSER_LABEL%
echo  API:    http://%OWNAPI_HOST%:%OWNAPI_PORT%
echo  Health: http://%OWNAPI_HOST%:%OWNAPI_PORT%/healthz
echo  UI:     http://%OWNAPI_HOST%:%OWNAPI_PORT%/ui
echo  Stop:   Ctrl+C
echo ============================================================
echo.

"%OWNAPI_PYTHON%" -m uvicorn app.main:app --host "%OWNAPI_HOST%" --port "%OWNAPI_PORT%" %*
set "OWNAPI_EXIT_CODE=%ERRORLEVEL%"

if not "%OWNAPI_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] OwnApi stopped with exit code %OWNAPI_EXIT_CODE%.
    pause
)

exit /b %OWNAPI_EXIT_CODE%
