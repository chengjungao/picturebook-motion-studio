@echo off
rem ================================================================
rem  Let the Picture Book Move  /  Windows launcher
rem
rem  Double-click this file. It will:
rem    1. start the local web interface
rem    2. open your browser
rem  Then use the four steps on the page. Nothing to install: this
rem  project uses only the Python standard library.
rem
rem  Close this window to stop the interface.
rem
rem  NOTE: this file is deliberately pure ASCII. Chinese text inside a
rem  .bat file gets garbled under the default 936 codepage, so every
rem  Chinese message is printed by Python instead (see app.py).
rem
rem  Options are passed straight through to app.py:
rem    --folder NAME     use another project folder
rem    --port 8770       pick the port yourself
rem    --no-browser      do not open a browser
rem ================================================================

chcp 65001 >nul
setlocal

where py >nul 2>nul
if not errorlevel 1 goto use_py

where python >nul 2>nul
if not errorlevel 1 goto use_python

goto no_python

:use_py
pushd "%~dp0"
py -3 app.py %*
set RC=%ERRORLEVEL%
popd
goto done

:use_python
pushd "%~dp0"
python app.py %*
set RC=%ERRORLEVEL%
popd
goto done

:no_python
echo.
echo   Python was not found.
echo.
echo   Install Python 3.8 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then run this file again.
echo.
echo   Already have Python? Then try running "python app.py" in a terminal
echo   to see the real error message.
echo.
set RC=1
goto done

:done
if not "%RC%"=="0" pause
endlocal
exit /b %RC%
