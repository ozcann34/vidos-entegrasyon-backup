@echo off
REM Vidos Entegrasyon Server Starter
REM This batch file starts the Flask development server

echo ========================================
echo    VIDOS ENTEGRASYON SERVER
echo ========================================
echo.
echo Starting Flask server...
echo Server will be available at: http://localhost:5000
echo.
echo Press Ctrl+C to stop the server
echo ========================================
echo.

REM Run the Flask application
python run.py

REM Keep the window open if there's an error
if errorlevel 1 (
    echo.
    echo ========================================
    echo ERROR: Server failed to start!
    echo ========================================
    pause
)
