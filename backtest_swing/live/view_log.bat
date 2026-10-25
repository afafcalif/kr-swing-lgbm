@echo off
rem Opens the log viewer window (read only; closing it does not affect the resident).
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0view_log.ps1"
