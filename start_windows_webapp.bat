@echo off
setlocal enabledelayedexpansion

echo ==============================================================================
echo Naukri Multi-User Automation Web App & Scheduler (Windows - Port 8080)
echo ==============================================================================
echo.

cd /d "%~dp0"

:: 1. Verify Python is installed
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not found in your Windows PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/ and check "Add Python to PATH".
    pause
    exit /b 1
)

:: 2. Install Playwright & Chromium if not already installed
echo [1/3] Checking & installing Playwright dependencies...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install playwright playwright-stealth
playwright install chromium

:: 3. Allow inbound connections on Port 8080 in Windows Firewall (if running as Admin)
echo.
echo [2/3] Ensuring Windows Firewall allows Port 8080...
netsh advfirewall firewall show rule name="NaukriWebApp8080" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    netsh advfirewall firewall add rule name="NaukriWebApp8080" dir=in action=allow protocol=TCP localport=8080 >nul 2>&1
)

:: 4. Launch the Web App on Port 8080 and open browser
echo.
echo [3/3] Starting Naukri Web Portal on http://localhost:8080 ...
start "" http://localhost:8080
python naukri_dashboard.py --port 8080

pause
