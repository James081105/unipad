@echo off
rem Thin double-clickable shim for scripts\windows\UniPad-Launcher.ps1
rem (PRD 3.2 P1 bullet 6).  Forwards every argument to the PowerShell launcher
rem and preserves its exit code.  -ExecutionPolicy Bypass is required because
rem the script is a local, unsigned file; -NoProfile keeps the run deterministic.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\UniPad-Launcher.ps1" %*
exit /b %ERRORLEVEL%
