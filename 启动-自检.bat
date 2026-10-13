@echo off
rem ================================================================
rem  Environment check only  /  no interface, no browser
rem
rem  Runs "app.py --check" and then "--plan", so you can see what the
rem  film's timeline would be before spending an hour on clips.
rem
rem  NOTE: pure ASCII on purpose. Chinese text inside a .bat file gets
rem  garbled under the default 936 codepage, so every Chinese message is
rem  printed by Python instead.
rem ================================================================

chcp 65001 >nul
setlocal

where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
goto no_python

:use_py
set PY=py -3
goto run

:use_python
set PY=python
goto run

:run
pushd "%~dp0"
%PY% app.py --check
echo.
%PY% app.py --plan
set RC=%ERRORLEVEL%
popd
goto done

:no_python
echo.
echo   Python was not found. Install Python 3.8 or newer first.
echo.
set RC=1

:done
echo.
pause
endlocal
exit /b %RC%
