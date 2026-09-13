@echo off
rem Descarga el dashboard mas reciente de GitHub Actions y lo abre en el navegador.
cd /d "%~dp0"
python -m tracker sync --open
if errorlevel 1 pause
