@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Movie Downloader Assist
if exist ".venv\Scripts\python.exe" goto launch
if /I "%~1"=="--check" goto missing_environment
echo Python environment not found. Running setup first...
call "%~dp0setup.bat" --no-pause
if errorlevel 1 goto failed
:launch
".venv\Scripts\python.exe" launch.py %*
if errorlevel 1 goto failed
exit /b 0
:missing_environment
echo Python environment not found. Run setup.bat before checking.
exit /b 1
:failed
echo.
echo Startup failed. Read the message above and README.md.
if /I not "%~1"=="--check" pause
exit /b 1
