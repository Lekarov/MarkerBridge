@echo off
REM Glissez un fichier .mp4 (ou un dossier de .mp4) sur ce .bat pour générer
REM le XML de marqueurs Premiere correspondant.

if "%~1"=="" (
    echo Aucun fichier/dossier fourni.
    echo Glissez un .mp4 ou un dossier sur ce fichier .bat pour l'utiliser.
    pause
    exit /b 1
)

python "%~dp0markerbridge.py" "%~1"

echo.
pause
