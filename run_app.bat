@echo off
setlocal
pushd "%~dp0"
set "PYTHON_EXE=%~dp0.venv-app\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Full application environment .venv-app was not found.
    echo Create it and install requirements.txt. See README.md.
    popd
    pause
    exit /b 1
)

echo [INFO] Starting current E03/E04 presentation interface...
"%PYTHON_EXE%" -m streamlit run "%~dp0current_app.py" --server.address 127.0.0.1 --browser.gatherUsageStats false
set "APP_EXIT=%ERRORLEVEL%"

if not "%APP_EXIT%"=="0" (
    echo.
    echo [ERROR] Application failed to start or was closed with an error.
    pause
)
popd
exit /b %APP_EXIT%
