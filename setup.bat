@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Movie Downloader Assist - Setup
if not exist ".venv\Scripts\python.exe" goto find_python
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 goto broken_environment
goto install
:find_python
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 goto create_with_py
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 goto create_with_python
echo Python 3.10 or newer is required. Install Python from python.org.
echo Enable the Python launcher or Add Python to PATH, then run setup.bat again.
goto failed
:create_with_py
py -3 -m venv .venv
if errorlevel 1 goto failed
goto install
:create_with_python
python -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto install_failed
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failed
".venv\Scripts\python.exe" launch.py --setup
if errorlevel 1 goto failed
if /I not "%~1"=="--no-pause" pause
exit /b 0
:broken_environment
echo The existing .venv is broken or uses Python older than 3.10.
echo Rename .venv manually, then run setup.bat again. Your data is in data, not .venv.
goto failed
:install_failed
echo Dependency installation failed. Check the network and the pip error above.
echo Existing config.json and data are preserved. Run setup.bat again after fixing it.
:failed
if /I not "%~1"=="--no-pause" pause
exit /b 1
