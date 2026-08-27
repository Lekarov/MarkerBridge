@echo off
REM Lance MarkerBridge (interface graphique de conversion des chapitres OBS
REM en marqueurs Premiere). Verifie que Python et tkinter sont disponibles.

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python est introuvable sur ce systeme.
    echo Installez Python 3 depuis https://www.python.org/downloads/
    echo puis relancez ce fichier.
    pause
    exit /b 1
)

python -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo Le module tkinter est introuvable dans votre installation Python.
    echo Reinstallez Python en cochant l'option "tcl/tk and IDLE" a l'installation.
    pause
    exit /b 1
)

REM pythonw evite d'ouvrir une console derriere l'interface, si disponible.
where pythonw >nul 2>&1
if errorlevel 1 (
    start "" python "%~dp0gui.py"
) else (
    start "" pythonw "%~dp0gui.py"
)
