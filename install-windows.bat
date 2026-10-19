@echo off
setlocal DisableDelayedExpansion
REM PY_VERSION: require working 64-bit Python 3.10+, not a Store alias.
pushd "%~dp0"
if errorlevel 1 exit /b 1
python -c "import sys,struct; sys.exit(not (sys.version_info >= (3,10) and struct.calcsize('P') == 8))" >nul 2>nul
if not errorlevel 1 goto use_python
py -3 -c "import sys,struct; sys.exit(not (sys.version_info >= (3,10) and struct.calcsize('P') == 8))" >nul 2>nul
if not errorlevel 1 goto use_launcher
echo [install-windows] ERROR: Install 64-bit Python 3.10+ and enable Add Python to PATH.
set "INSTALL_RC=1"
goto finish
:use_python
python scripts\install_windows.py
set "INSTALL_RC=%ERRORLEVEL%"
goto finish
:use_launcher
py -3 scripts\install_windows.py
set "INSTALL_RC=%ERRORLEVEL%"
:finish
popd
if /i "%~1"=="--no-pause" goto done
if defined CI goto done
pause
:done
exit /b %INSTALL_RC%
