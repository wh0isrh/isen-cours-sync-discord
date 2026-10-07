@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "RCLONE_BIN=rclone"
where rclone >nul 2>nul
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.75.1-windows-amd64\rclone.exe" (
        set "RCLONE_BIN=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.75.1-windows-amd64\rclone.exe"
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Le venv Python est absent. Lancer setup_windows.bat une premi?re fois.
    pause
    exit /b 2
)
call ".venv\Scripts\activate.bat"
python "sync_moodle.py" %*
set "SYNC_EXIT_CODE=%ERRORLEVEL%"

if %SYNC_EXIT_CODE% NEQ 0 goto end_script
if not exist "..\ISEN_Lille_2026-2027" goto end_script

echo.
echo ========================================================
echo Envoi instantan? vers OneDrive avec Rclone...
echo ========================================================
"%RCLONE_BIN%" copy "..\ISEN_Lille_2026-2027" "onedrive:Cours_ISEN/ISEN_Lille_2026-2027" --transfers 4 --fast-list
echo Synchronisation OneDrive termin?e avec succ?s !

:end_script
echo.
echo Fin de l'ex?cution. Code de sortie : %SYNC_EXIT_CODE%
pause
exit /b %SYNC_EXIT_CODE%