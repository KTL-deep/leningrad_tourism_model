@echo off
set "VENV_PATH=.venv\Scripts\activate"

if exist "%VENV_PATH%" (
    echo [INFO] Activating virtual environment...
    call "%VENV_PATH%"
) else (
    echo [WARN] Virtual environment not found, using global python/streamlit...
)

echo [INFO] Starting Streamlit application...
streamlit run app.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Application failed to start or was closed with an error.
    pause
)
