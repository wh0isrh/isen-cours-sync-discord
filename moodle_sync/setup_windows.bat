@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    echo Installer Python 3.10 ou plus récent depuis python.org, puis relancer.
    pause
    exit /b 2
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv ".venv"
if errorlevel 1 goto :failed
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :failed
python -m pip install -r requirements.txt
if errorlevel 1 goto :failed
python -m playwright install chromium
if errorlevel 1 goto :failed
if not exist ".env" copy /y ".env.example" ".env" >nul
echo.
echo Installation terminée. Renseigner .env puis lancer sync_moodle.bat.
pause
exit /b 0
:failed
echo Installation interrompue. Voir l'erreur ci-dessus.
pause
exit /b 1
