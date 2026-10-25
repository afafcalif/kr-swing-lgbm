@echo off
rem Stop: stop flag (watchdog will not restart) -> wait for normal exit -> force kill only after timeout -> clean lock files.
chcp 65001 >nul
cd /d "%~dp0..\.."
set "PY=%CD%\.venv-feat\Scripts\python.exe"
if defined STOCK_PYTHON set "PY=%STOCK_PYTHON%"
set PYTHONIOENCODING=utf-8
"%PY%" backtest_swing\live\control.py stop
if /i not "%~1"=="nopause" pause
