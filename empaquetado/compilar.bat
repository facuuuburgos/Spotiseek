@echo off
chcp 65001 >nul
echo === Instalando herramientas de empaquetado ===
python -m pip install --upgrade pyinstaller yt-dlp customtkinter requests pillow
echo.
echo === Creando el programa (puede tardar unos minutos) ===
cd /d "%~dp0.."
python -m PyInstaller --noconfirm --clean --windowed --name Spotiseek ^
  --icon assets\icono.ico --add-data "assets;assets" ^
  --collect-all customtkinter --collect-all yt_dlp ^
  lanzador.py
if errorlevel 1 (
  echo.
  echo *** ERROR al crear el programa. Copia el mensaje de arriba y pegalo en el chat. ***
  pause
  exit /b 1
)

echo.
echo === Copiando slskd (SIN tu slskd.yml ni tus datos) y ffmpeg ===
if not exist slskd\slskd.exe (
  echo *** No encuentro slskd\slskd.exe en esta carpeta. ***
  pause
  exit /b 1
)
robocopy slskd dist\Spotiseek\slskd /E /XF slskd.yml slskd.yml.bak *.log *.db *.db-shm *.db-wal /XD logs data downloads incomplete >nul
if errorlevel 8 (
  echo *** Error copiando slskd ***
  pause
  exit /b 1
)
if exist ffmpeg\ffmpeg.exe (
  robocopy ffmpeg dist\Spotiseek\ffmpeg /E >nul
) else (
  echo *** AVISO: falta ffmpeg\ffmpeg.exe y ffmpeg\ffprobe.exe: no funcionara YouTube. ***
)

REM Seguridad: que no viaje ninguna configuracion personal
if exist dist\Spotiseek\slskd\slskd.yml del /q dist\Spotiseek\slskd\slskd.yml

echo.
echo === Listo: dist\Spotiseek\Spotiseek.exe ===
echo Prueba el programa y despues abre instalador.iss con Inno Setup (F9).
pause
