@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   EA License Server  (minimal, stdlib only)
echo ============================================================
echo.

set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE ( where py >nul 2>nul && set "PYEXE=py" )
if not defined PYEXE (
  echo [x] Python not found in PATH. Install Python 3.8+ first.
  pause
  exit /b 1
)

echo [i] Python: %PYEXE%
echo.

if not exist "licenses.db" (
  echo [i] first run: initializing database
  %PYEXE% license_server.py init
  echo.
)

echo [i] starting server on 0.0.0.0:8787
echo     run this file with an argument to override, e.g.:
echo         %~nx0 --port 9000 --secret MyLongSecret
echo.
echo     issue a license key in another window:
echo         %PYEXE% license_server.py issue --product GridMaster --days 365 --note "customer name"
echo.

%PYEXE% license_server.py serve --host 0.0.0.0 --port 8787 %*

echo.
echo [i] server stopped.
pause
