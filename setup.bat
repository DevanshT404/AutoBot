@echo off
cd /d "%~dp0"
echo === BrowserBot setup (run this once) ===

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PY%"=="" goto nopython

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto notk

%PY% -m venv .venv
if errorlevel 1 goto fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto fail
playwright install chromium
if errorlevel 1 goto fail

echo.
echo Setup complete. Double-click run.bat to start BrowserBot.
pause
exit /b 0

:nopython
echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
pause
exit /b 1

:notk
echo Your Python has no Tkinter. Re-run the python.org installer, choose Modify,
echo and make sure "tcl/tk and IDLE" is ticked. Then run setup.bat again.
pause
exit /b 1

:fail
echo Setup failed - see the messages above.
pause
exit /b 1
