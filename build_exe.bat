@echo off
REM Builds dist\RBCAgent\RBCAgent.exe with the config and data folders next to it.
cd /d "%~dp0"
echo Installing PyInstaller...
python -m pip install --upgrade pyinstaller || goto :fail
echo Building...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name RBCAgent main.py || goto :fail
set OUT=dist\RBCAgent
if not exist "%OUT%" mkdir "%OUT%"
move /Y dist\RBCAgent.exe "%OUT%\RBCAgent.exe" >nul
xcopy /E /I /Y config "%OUT%\config" >nul
xcopy /E /I /Y data "%OUT%\data" >nul
if not exist "%OUT%\logs" mkdir "%OUT%\logs"
if not exist "%OUT%\debug" mkdir "%OUT%\debug"
echo.
echo Done! Your program is in: %CD%\%OUT%\RBCAgent.exe
echo Keep the config and data folders next to the exe.
explorer "%OUT%"
pause
exit /b 0
:fail
echo.
echo Build failed - send a screenshot of the error above.
pause
exit /b 1
