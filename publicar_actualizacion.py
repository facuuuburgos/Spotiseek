"""
Prepara una actualización para publicar.

Uso (desde la carpeta del proyecto):
    python empaquetado\\publicar_actualizacion.py "Texto con las novedades"

Antes de ejecutarlo, sube VERSION en actualizador.py (por ejemplo "1.0.0" -> "1.0.1").
Crea la carpeta "publicacion" con:
    actualizacion.zip   (gui.py, downloader.py, youtube.py, actualizador.py + iconos de assets)
    version.json        (versión, dirección de descarga, novedades y control sha256)
Sube esos dos archivos al mismo lugar donde apunta URL_VERSION.
"""

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVOS = ["gui.py", "downloader.py", "youtube.py", "actualizador.py"]

# Imágenes e iconos que también viajan en la actualización (se toman de la carpeta assets).
RECURSOS = ["icono.ico", "logo.png", "logo_128.png", "logo_64.png", "logo_simbolo.png"]

texto = (RAIZ / "actualizador.py").read_text(encoding="utf-8")

version = re.search(r'^VERSION\s*=\s*"([^"]+)"', texto, re.M).group(1)
url_version = re.search(r'^URL_VERSION\s*=\s*"([^"]*)"', texto, re.M).group(1).strip()

if not url_version:
    sys.exit("Falta configurar URL_VERSION en actualizador.py (ver la guía).")

url_zip = url_version.rsplit("/", 1)[0] + "/actualizacion.zip"

notas = " ".join(sys.argv[1:]).strip()

salida = RAIZ / "publicacion"
salida.mkdir(exist_ok=True)

zip_ruta = salida / "actualizacion.zip"

with zipfile.ZipFile(zip_ruta, "w", zipfile.ZIP_DEFLATED) as z:
    for nombre in ARCHIVOS:
        z.write(RAIZ / nombre, nombre)

    for nombre in RECURSOS:
        ruta = RAIZ / "assets" / nombre
        if ruta.exists():
            z.write(ruta, "assets/" + nombre)

sha = hashlib.sha256(zip_ruta.read_bytes()).hexdigest()

(salida / "version.json").write_text(
    json.dumps({"version": version, "url": url_zip, "notas": notas, "sha256": sha},
               ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Versión {version} lista en: {salida}")
print("Sube estos 2 archivos:  actualizacion.zip  y  version.json")
