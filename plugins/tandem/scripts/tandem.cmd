@echo off
rem Runs Tandem's control script: tandem.cmd status ^| start ^| stop ^| enable-desktop ^| restore-desktop ^| mcp
setlocal
set "PY=%TANDEM_PYTHON%"
rem Codex desktop ships a Python runtime that already includes the cryptography package.
if not defined PY if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PY set "PY=python"
"%PY%" -B "%~dp0control.py" %*
