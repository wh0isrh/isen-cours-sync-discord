@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Le venv Python est absent. Lancer setup_windows.bat une première fois.
    pause
    exit /b 2
)
call ".venv\Scripts\activate.bat"
python "sync_moodle.py" %*
set "SYNC_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Fin de l'exécution. Code de sortie : %SYNC_EXIT_CODE%
pause
exit /b %SYNC_EXIT_CODE%
