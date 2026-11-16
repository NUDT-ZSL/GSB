@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

%PYTHON% -c "import sys, sqlite3; print('Python', sys.version); print('SQLite', sqlite3.sqlite_version); assert sys.version_info >= (3, 10), 'Python 3.10+ required'; print('sqlite3: OK')"
if errorlevel 1 goto :fail

%PYTHON% -c "import tkinter; print('Tk', tkinter.TkVersion); print('tkinter: OK')"
if errorlevel 1 goto :tkfail

echo.
echo Environment check passed.
pause
exit /b 0

:tkfail
echo.
echo tkinter is unavailable.
echo Re-run the Python installer, choose Modify, and enable "tcl/tk and IDLE".
pause
exit /b 2

:fail
echo.
echo Python 3.10 or newer is required.
pause
exit /b 1

