@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0dist\MultiDL.exe" (
  start "" "%~dp0dist\MultiDL.exe" %*
  exit /b 0
)

set "PY="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not defined PY (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PY set "PY=%%i"
)
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
)
if not defined PY (
  echo [x] 没找到 Python，也没有已打包的 exe。
  pause
  exit /b 1
)

start "" "%PY%" "%~dp0main.py" %*
exit /b 0
