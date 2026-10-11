"""
Actualizaciones de la app.

Cómo funciona:
  1) La app descarga un archivo pequeño (version.json) desde URL_VERSION.
  2) Si la versión publicada es mayor que VERSION, ofrece actualizar.
  3) Se descarga un .zip con los archivos .py nuevos y se guardan en la carpeta
     "actualizacion" junto al programa. Al abrir, el lanzador usa esos archivos.

NOTA: cada vez que publiques cambios, sube VERSION (por ejemplo 1.0.1 -> 1.0.2).
"""

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# ----------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------

VERSION = "1.0.1"

# Dirección del archivo version.json publicado (ver la guía de publicación).
# Ejemplo con GitHub:
#   https://raw.githubusercontent.com/TU_USUARIO/TU_REPO/main/version.json
# Si está vacío, la app no busca actualizaciones.
URL_VERSION = "https://github.com/facuuuburgos/Spotiseek/releases/latest/download/version.json"

# Archivos que una actualización puede reemplazar (nada más).
ARCHIVOS_PERMITIDOS = {
    "gui.py",
    "downloader.py",
    "youtube.py",
    "actualizador.py",
}

CARPETA_ACTUALIZACION = "actualizacion"
TIMEOUT_SEG = 15


# ----------------------------------------------------------------------
# UTILIDADES
# ----------------------------------------------------------------------

def _como_tupla(texto):
    partes = []
    for trozo in str(texto).strip().lstrip("vV").split("."):
        digitos = "".join(c for c in trozo if c.isdigit())
        partes.append(int(digitos) if digitos else 0)
    while len(partes) < 3:
        partes.append(0)
    return tuple(partes)


def es_mayor(remota, local):
    return _como_tupla(remota) > _como_tupla(local)


def carpeta_actualizacion(base_dir):
    return Path(base_dir) / CARPETA_ACTUALIZACION


def _pedir(url):
    peticion = urllib.request.Request(url, headers={"User-Agent": "SpotifySoulseekApp"})
    return urllib.request.urlopen(peticion, timeout=TIMEOUT_SEG)


# ----------------------------------------------------------------------
# BUSCAR
# ----------------------------------------------------------------------

def buscar_actualizacion(url_version=None, version_actual=None):
    """
    Devuelve un diccionario con la actualización disponible
    ({'version', 'url', 'notas', 'sha256'}) o None si no hay nada nuevo.
    Lanza una excepción si no se pudo consultar.
    """

    url_version = (url_version if url_version is not None else URL_VERSION).strip()
    version_actual = version_actual or VERSION

    if not url_version:
        return None

    with _pedir(url_version) as r:
        datos = json.loads(r.read().decode("utf-8-sig"))

    version = str(datos.get("version", "")).strip()
    url = str(datos.get("url", "")).strip()

    if not version or not url:
        raise ValueError("version.json no tiene 'version' o 'url'.")

    if not es_mayor(version, version_actual):
        return None

    return {
        "version": version,
        "url": url,
        "notas": str(datos.get("notas", "")).strip(),
        "sha256": str(datos.get("sha256", "")).strip().lower(),
    }


# ----------------------------------------------------------------------
# INSTALAR
# ----------------------------------------------------------------------

def instalar_actualizacion(info, base_dir, progreso=None):
    """
    Descarga y deja listos los archivos nuevos. Devuelve la lista de archivos
    instalados. Lanza una excepción si algo sale mal (en ese caso no se cambia nada).
    """

    def avisar(texto):
        if progreso:
            try:
                progreso(texto)
            except Exception:
                pass

    destino = carpeta_actualizacion(base_dir)

    with tempfile.TemporaryDirectory() as tmp:

        tmp = Path(tmp)
        zip_local = tmp / "actualizacion.zip"

        avisar("Descargando actualización...")

        sha = hashlib.sha256()

        with _pedir(info["url"]) as r, open(zip_local, "wb") as f:
            while True:
                bloque = r.read(65536)
                if not bloque:
                    break
                f.write(bloque)
                sha.update(bloque)

        if info.get("sha256") and sha.hexdigest().lower() != info["sha256"]:
            raise ValueError("El archivo descargado está dañado (no coincide el control sha256).")

        avisar("Comprobando archivos...")

        nuevos = tmp / "nuevos"
        nuevos.mkdir()

        with zipfile.ZipFile(zip_local) as z:

            for nombre in z.namelist():

                limpio = Path(nombre).name  # ignora carpetas dentro del zip

                if limpio not in ARCHIVOS_PERMITIDOS or nombre.endswith("/"):
                    continue

                (nuevos / limpio).write_bytes(z.read(nombre))

        archivos = sorted(p.name for p in nuevos.iterdir())

        if not archivos:
            raise ValueError("El zip no contiene archivos de la app.")

        # Que no tengan errores de sintaxis antes de instalarlos.
        for nombre in archivos:
            compile((nuevos / nombre).read_text(encoding="utf-8"), nombre, "exec")

        avisar("Instalando...")

        destino.mkdir(parents=True, exist_ok=True)

        # Copia de seguridad de lo anterior (para volver atrás si algo falla).
        respaldo = destino.parent / (CARPETA_ACTUALIZACION + "_anterior")

        if respaldo.exists():
            shutil.rmtree(respaldo, ignore_errors=True)

        if any(destino.iterdir()):
            shutil.copytree(destino, respaldo)

        for nombre in archivos:
            shutil.copy2(nuevos / nombre, destino / nombre)

        (destino / "version.txt").write_text(info["version"], encoding="utf-8")

    return archivos


def descartar_actualizacion(base_dir):
    """Borra los archivos actualizados: la app vuelve a la versión del instalador."""
    shutil.rmtree(carpeta_actualizacion(base_dir), ignore_errors=True)
