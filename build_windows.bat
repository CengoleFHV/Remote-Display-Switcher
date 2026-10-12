@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
    echo Fehler: .venv\Scripts\pyinstaller.exe wurde nicht gefunden.
    exit /b 1
)

".venv\Scripts\pyinstaller.exe" --clean --noconfirm PCDisplaySwitch.spec
if errorlevel 1 (
    echo Fehler: Der Windows-Build ist fehlgeschlagen.
    exit /b 1
)

echo.
echo Build erfolgreich: dist\PCDisplaySwitch.exe
echo MultiMonitorTool.exe fuer die lokale Nutzung separat daneben ablegen.
