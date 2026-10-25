@echo off
rem Update code (sub PC). Refused on trading days 08:00-15:45.
rem stop resident -> git pull --ff-only -> frozen code check -> all tests -> start (on test failure: back to previous commit, then start).
chcp 65001 >nul
cd /d "%~dp0..\.."
set "PY=%CD%\.venv-feat\Scripts\python.exe"
if defined STOCK_PYTHON set "PY=%STOCK_PYTHON%"
set PYTHONIOENCODING=utf-8
"%PY%" backtest_swing\live\update.py
set RC=%ERRORLEVEL%
if /i not "%~1"=="nopause" pause
exit /b %RC%
