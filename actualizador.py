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

VERSION = "1.0.2"

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


def _pedir(url, timeout=None):
    peticion = urllib.request.Request(url, headers={"User-Agent": "SpotifySoulseekApp"})
    return urllib.request.urlopen(peticion, timeout=timeout or TIMEOUT_SEG)


# ----------------------------------------------------------------------
# BUSCAR
# ----------------------------------------------------------------------

def buscar_actualizacion(url_version=None, version_actual=None, timeout=None):
    """
    Devuelve un diccionario con la actualización disponible
    ({'version', 'url', 'notas', 'sha256'}) o None si no hay nada nuevo.
    Lanza una excepción si no se pudo consultar.
    """

    url_version = (url_version if url_version is not None else URL_VERSION).strip()
    version_actual = version_actual or VERSION

    if not url_version:
        return None

    with _pedir(url_version, timeout) as r:
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



# ======================================================================
# CUENTAS: registro de usuarios y bloqueos
# ======================================================================
# (Está en este archivo a propósito: así también lo reciben las instalaciones
#  antiguas por actualización automática.)
#
# Se envía a TU servidor (Supabase): un identificador de instalación, el
# USUARIO de Soulseek (nunca la contraseña), la versión de la app y el sistema.
# El servidor anota la dirección IP por su cuenta. Con eso puedes ver quién
# usa la app y suspender a alguien desde el panel de administración.
#
# Se rellena una sola vez, con los datos de tu proyecto de Supabase
# (ver la guía GUIA_ADMINISTRACION.md). Si está vacío, el sistema queda
# apagado y la app funciona como siempre.

SUPABASE_URL = "https://qanolcdlreclfpkthhel.supabase.co"
SUPABASE_CLAVE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFhbm9sY2RscmVjbGZwa3RoaGVsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTE3NTEzMzksImV4cCI6MjEwNzMyNzMzOX0.wDD0iWyKo7F_iQFEYySa6jCvRX8AUUpsPeZ6jUci4v8"  # clave "anon" (pública, no es secreta)

ARCHIVO_INSTALACION = "instalacion.json"
TEXTO_PRIVACIDAD = (
    "Para poder mejorar Spotiseek y evitar usos indebidos, la app envía al "
    "creador estos datos cada vez que se abre:\n\n"
    "  \u2022 tu dirección IP\n"
    "  \u2022 tu nombre de usuario de Soulseek (nunca tu contraseña)\n"
    "  \u2022 la versión de la app y tu sistema operativo\n"
    "  \u2022 un identificador anónimo de esta instalación\n\n"
    "No se envía nada de tu música, tus listas ni tus contraseñas. El creador "
    "puede suspender el acceso a la app si detecta un mal uso.\n\n"
    "Si no aceptas, la app no se podrá usar."
)


def cuentas_activas():
    return bool(SUPABASE_URL.strip() and SUPABASE_CLAVE.strip())


CLAVE_REGISTRO = r"Software\Spotiseek"


def _leer_registro():
    """Copia de respaldo del estado (en el registro de Windows, usuario actual)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLAVE_REGISTRO) as k:
            return json.loads(winreg.QueryValueEx(k, "estado")[0])
    except Exception:
        return {}


def _guardar_registro(datos):
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLAVE_REGISTRO) as k:
            winreg.SetValueEx(k, "estado", 0, winreg.REG_SZ, json.dumps(datos))
    except Exception:
        pass


def _leer_instalacion(base_dir):
    """Estado local: se combinan el archivo y el registro (si falta uno, vale el otro)."""

    try:
        archivo = json.loads((Path(base_dir) / ARCHIVO_INSTALACION).read_text(encoding="utf-8"))
    except Exception:
        archivo = {}

    registro = _leer_registro()

    datos = dict(registro)
    datos.update({k: v for k, v in archivo.items() if v not in (None, "")})

    # Una suspensión anotada en cualquiera de los dos lugares cuenta.
    if registro.get("bloqueado") or archivo.get("bloqueado"):
        datos["bloqueado"] = True
        datos["motivo"] = archivo.get("motivo") or registro.get("motivo") or ""

    return datos


def _guardar_instalacion(base_dir, datos):
    try:
        (Path(base_dir) / ARCHIVO_INSTALACION).write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[CUENTAS] No se pudo guardar {ARCHIVO_INSTALACION}: {e}")

    _guardar_registro(datos)


def huella_equipo():
    """
    Código que identifica a esta computadora (hash del identificador que Windows
    genera al instalarse). No se envía el identificador original, solo el hash.
    """

    import hashlib

    crudo = ""

    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as k:
            crudo = str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:
        for ruta in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                crudo = Path(ruta).read_text().strip()
                break
            except Exception:
                pass

    if not crudo:
        return ""

    return hashlib.sha256(("spotiseek|" + crudo).encode("utf-8")).hexdigest()[:40]


def consentimiento_dado(base_dir):
    return bool(_leer_instalacion(base_dir).get("consentimiento"))


def guardar_consentimiento(base_dir):
    datos = _leer_instalacion(base_dir)
    datos["consentimiento"] = True
    _guardar_instalacion(base_dir, datos)


def id_instalacion(base_dir):
    datos = _leer_instalacion(base_dir)

    if not datos.get("id"):
        import uuid
        datos["id"] = uuid.uuid4().hex
        _guardar_instalacion(base_dir, datos)

    return datos["id"]


def registrar_inicio(base_dir, usuario_soulseek, timeout=8, solo_comprobar=False):
    """
    Avisa al servidor de que la app se abrió y pregunta si el acceso está suspendido.

    Devuelve {'bloqueado': bool, 'motivo': str}.
    Si no hay conexión con el servidor devuelve {'bloqueado': <lo último que se supo>,
    'motivo': ..., 'sin_conexion': True} (no se deja fuera a nadie por una caída de internet,
    salvo que ya estuviera suspendido la última vez).
    Si el sistema no está configurado devuelve None.
    """

    if not cuentas_activas():
        return None

    import platform

    datos = _leer_instalacion(base_dir)

    parametros = {
        "p_instalacion": id_instalacion(base_dir),
        "p_usuario": str(usuario_soulseek or ""),
        "p_huella": huella_equipo(),
    }

    if not solo_comprobar:
        parametros["p_version"] = VERSION
        parametros["p_sistema"] = f"{platform.system()} {platform.release()}"

    cuerpo = json.dumps(parametros).encode("utf-8")

    funcion = "comprobar" if solo_comprobar else "registrar"
    url = SUPABASE_URL.strip().rstrip("/") + "/rest/v1/rpc/" + funcion

    peticion = urllib.request.Request(
        url,
        data=cuerpo,
        method="POST",
        headers={
            "apikey": SUPABASE_CLAVE.strip(),
            **({} if SUPABASE_CLAVE.strip().startswith("sb_") else {"Authorization": "Bearer " + SUPABASE_CLAVE.strip()}),
            "Content-Type": "application/json",
            "User-Agent": "Spotiseek",
        },
    )

    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))

        resultado = {
            "bloqueado": bool(resp.get("bloqueado")),
            "motivo": str(resp.get("motivo") or "").strip(),
        }

        datos = _leer_instalacion(base_dir)
        datos["bloqueado"] = resultado["bloqueado"]
        datos["motivo"] = resultado["motivo"]
        _guardar_instalacion(base_dir, datos)

        return resultado

    except Exception as e:
        print(f"[CUENTAS] No se pudo contactar con el servidor: {e}")

        return {
            "bloqueado": bool(datos.get("bloqueado")),
            "motivo": str(datos.get("motivo") or ""),
            "sin_conexion": True,
        }


def comprobar_suspension(base_dir, usuario_soulseek, timeout=8):
    """Consulta rápida (no anota nada) para saber si te suspendieron con la app abierta."""
    return registrar_inicio(base_dir, usuario_soulseek, timeout=timeout, solo_comprobar=True)
