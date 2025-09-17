@echo off
echo Building Web Cloner Executable...
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH
    pause
    exit /b 1
)

REM Install required packages
echo Installing required packages...
pip install -r requirements-full.txt
pip install pyinstaller

REM Build the executable
echo Building executable...
python build_exe.py

REM Check if build was successful
if exist "dist\WebCloner.exe" (
    echo.
    echo SUCCESS: WebCloner.exe created in the dist folder!
    echo You can now run dist\WebCloner.exe
) else (
    echo.
    echo ERROR: Build failed. Check the output above for errors.
)

echo.
pause
