@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set /p NOTAS=Que cambio en esta version? (una linea): 
python empaquetado\publicar_actualizacion.py "%NOTAS%"
pause
