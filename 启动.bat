@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYEXE=
where py >nul 2>nul && set PYEXE=py -3
if "%PYEXE%"=="" (where python >nul 2>nul && set PYEXE=python)
if "%PYEXE%"=="" (
  echo [x] Python not found. Please install Python 3.8+ first.
  pause
  exit /b 3
)
echo Starting guard launcher ...
%PYEXE% guard_launcher.py --config guard.json
echo.
echo (launcher exited, code=%ERRORLEVEL%)
pause
