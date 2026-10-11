import json
import sys
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter


# ============================================================
# CONFIGURACIÓN
# ============================================================

EXTENSIONES_AUDIO = {
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".alac",
    ".aiff",
    ".aif",
}

MAX_CANDIDATOS = 10

# Segundos que se espera a que slskd.exe termine de arrancar.
ESPERA_ARRANQUE_SLSKD_SEG = 90

# Si ya hay un slskd.exe abierto pero no responde, segundos que se le da para
# que termine de arrancar antes de cerrarlo y abrir uno nuevo.
ESPERA_INSTANCIA_EXISTENTE_SEG = 20

# True: al abrir la app SIEMPRE se cierra cualquier slskd.exe que haya abierto y
# se inicia uno nuevo con tu configuración (slskd.yml). Más lento al abrir, pero
# garantiza que corre con tu configuración. False: se reutiliza el que ya responde.
REINICIAR_SLSKD_AL_ABRIR = False

# Cuántas canciones se procesan al mismo tiempo (búsqueda + descarga).
# Subirlo acelera el proceso, pero demasiadas búsquedas simultáneas
# pueden hacer que la red de Soulseek te limite. 6 es un buen punto de partida.
MAX_SIMULTANEAS = 6

# Cuántas canciones pueden estar a la vez en proceso (buscando o descargando).
# MAX_SIMULTANEAS limita solo las que están buscando: apenas una empieza a
# descargar, deja su lugar a la siguiente búsqueda.
MAX_EN_PROCESO = 14

# La búsqueda comienza a procesarse inmediatamente.
# Máximo de segundos de búsqueda ACTIVA (desde que slskd la ejecuta de verdad).
# Pasado ese tiempo se DETIENE la búsqueda y se leen los resultados que llegaron.
# (slskd solo entrega los resultados cuando la búsqueda termina.)
ESPERA_BUSQUEDA_SEG = 25
# Si pasan estos segundos sin que llegue ninguna respuesta nueva, se da por terminada.
SEGUNDOS_SILENCIO_BUSQUEDA = 8
# Tiempo de gracia para que slskd entregue los resultados tras detener la búsqueda.
SEGUNDOS_GRACIA_DETENCION = 8
# Segundos sin respuestas nuevas con los que slskd da por terminada la búsqueda (por defecto 15000 ms).
BUSQUEDA_INACTIVIDAD_MS = 10000
# slskd hace fila con las búsquedas (estado 'Queued'). Máximo que esperamos en esa fila.
ESPERA_COLA_SEG = 300
INTERVALO_BUSQUEDA_SEG = 1.0
SEGUNDOS_ESPERAR_MEJOR_MATCH = 5

PREFERIR_EXTENDED_MIX = True

# MP3 válido solamente si slskd informa EXACTAMENTE 320 kbps.
BITRATE_MINIMO_MP3 = 320

TIMEOUT_DESCARGA_SEG = 600

# ------------------------------------------------------------
# Carrera de candidatos y detección de descargas estancadas
# ------------------------------------------------------------

# Cuántos matches se piden a la vez para la MISMA canción. Gana el primero que
# empieza a transferir datos; los demás se cancelan. Poner 1 para ir de a uno.
CANDIDATOS_EN_CARRERA = 3

# True: solo compiten entre sí matches de la misma calidad (por ejemplo, todos
# Extended MP3 320). Los de menor calidad se prueban recién si los mejores
# fallan. False: compiten todos y gana el más rápido, sin importar la calidad.
CARRERA_SOLO_MISMA_CALIDAD = True

# Si un match no empezó a transferir datos en este tiempo (por ejemplo, está
# esperando en una cola remota), se descarta y se prueba otro.
SEGUNDOS_SIN_ARRANQUE = 60

# Si un match ya estaba transfiriendo y el porcentaje no avanza en este tiempo,
# se descarta y se prueba otro.
SEGUNDOS_SIN_PROGRESO = 30

MOSTRAR_RESULTADOS_RAW = False

API_BASE = "http://127.0.0.1:5030/api/v0"


# ============================================================
# CONTROL DE DESCARGA
# ============================================================

class ControlDescarga:

    def __init__(self):
        self.cancelar = threading.Event()
        self.siguiente = threading.Event()


# ============================================================
# AJUSTES DEL USUARIO (se guardan en ajustes.json junto a la app)
# ============================================================

AJUSTES_PREDETERMINADOS = {
    # "" = la carpeta «downloads» de la app
    "carpeta_descargas": "",
    # "titulo_artista" -> «Canción - Artista»   |   "artista_titulo" -> «Artista - Canción»
    "formato_nombre": "titulo_artista",
    "seg_sin_arranque": 60,
    "seg_sin_progreso": 30,
    "espera_busqueda": 25,
    "candidatos_en_carrera": 3,
    "max_simultaneas": 6,
    "preferir_extended": True,
    "youtube_respaldo": False,
    "youtube_bitrate": 320,
    "confirmar_cierre": True,
    "buscar_actualizaciones": True,
    "abrir_carpeta_al_terminar": False,
}

# (mínimo, máximo) de los ajustes numéricos
LIMITES_AJUSTES = {
    "seg_sin_arranque": (10, 900),
    "seg_sin_progreso": (10, 600),
    "espera_busqueda": (8, 120),
    "candidatos_en_carrera": (1, 6),
    "max_simultaneas": (1, 12),
}

BITRATES_YOUTUBE = (128, 192, 256, 320)

EXTENSIONES_AUDIO_FINALES = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".wma", ".aiff", ".aif", ".alac", ".ape"}


def glob_literal(texto):
    """Escapa los caracteres especiales de glob ([ ] * ?) para buscar un nombre tal cual."""
    return "".join(f"[{c}]" if c in "[]*?" else c for c in str(texto))

FORMATO_NOMBRE = "titulo_artista"


class Ajustes:
    """Lee y guarda las preferencias del usuario."""

    def __init__(self, base_dir):
        self.archivo = Path(base_dir) / "ajustes.json"
        self.datos = dict(AJUSTES_PREDETERMINADOS)
        self.cargar()

    def cargar(self):

        try:
            if self.archivo.exists():
                crudo = json.loads(self.archivo.read_text(encoding="utf-8"))

                if isinstance(crudo, dict):
                    for clave, valor in crudo.items():
                        if clave in AJUSTES_PREDETERMINADOS:
                            self.datos[clave] = valor

        except Exception as e:
            print(f"[AJUSTES] No se pudo leer ajustes.json: {e}")

        self.datos = self._validar(self.datos)

    @staticmethod
    def _validar(datos):
        """Deja cada ajuste con un valor válido (si no, el predeterminado)."""

        limpio = dict(AJUSTES_PREDETERMINADOS)

        for clave, defecto in AJUSTES_PREDETERMINADOS.items():

            valor = datos.get(clave, defecto)

            try:

                if isinstance(defecto, bool):
                    valor = bool(valor)

                elif clave in LIMITES_AJUSTES:
                    bajo, alto = LIMITES_AJUSTES[clave]
                    valor = max(bajo, min(alto, int(valor)))

                elif clave == "youtube_bitrate":
                    valor = int(valor)
                    if valor not in BITRATES_YOUTUBE:
                        valor = defecto

                elif clave == "formato_nombre":
                    if valor not in ("titulo_artista", "artista_titulo"):
                        valor = defecto

                else:
                    valor = str(valor or "").strip()

            except Exception:
                valor = defecto

            limpio[clave] = valor

        return limpio

    def get(self, clave):
        return self.datos.get(clave, AJUSTES_PREDETERMINADOS.get(clave))

    def actualizar(self, nuevos):
        """Valida, guarda y devuelve True si se pudo escribir el archivo."""

        combinados = dict(self.datos)
        combinados.update(nuevos or {})

        self.datos = self._validar(combinados)

        try:
            self.archivo.write_text(
                json.dumps(self.datos, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return True

        except Exception as e:
            print(f"[AJUSTES] No se pudo guardar ajustes.json: {e}")
            return False


def construir_nombre(artista, titulo, formato=None):
    """Nombre de archivo (sin extensión): «Canción - Artista» o «Artista - Canción»."""

    artista = str(artista or "").replace("\xa0", " ").strip()
    titulo = str(titulo or "").replace("\xa0", " ").strip()

    formato = formato or FORMATO_NOMBRE

    partes = [artista, titulo] if formato == "artista_titulo" else [titulo, artista]

    nombre = " - ".join(p for p in partes if p)

    nombre = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nombre)
    nombre = re.sub(r"\s+", " ", nombre).strip(" .")

    return nombre[:180].strip(" .") or "cancion"


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_nombre_archivo(nombre):

    nombre = str(nombre or "").strip()
    nombre = nombre.replace("\\", "/")
    nombre = nombre.split("/")[-1]
    nombre = re.sub(r'[<>:"/\\|?*]', "_", nombre)
    nombre = nombre.strip(" .")

    if not nombre:
        nombre = "archivo"

    return nombre


# ============================================================
# DOWNLOADER
# ============================================================

class SoulseekDownloader:

    def __init__(self, base_dir=None, slskd_url=API_BASE):

        if base_dir is None:
            if getattr(sys, "frozen", False):
                # Programa empaquetado (.exe): la carpeta de la app es la del ejecutable.
                base_dir = Path(sys.executable).resolve().parent
            else:
                base_dir = Path(__file__).resolve().parent

        self.base_dir = Path(base_dir)
        self.slskd_dir = self.base_dir / "slskd"
        self.slskd_exe = self.slskd_dir / "slskd.exe"
        # Carpeta interna donde slskd deja lo que baja (con sus subcarpetas).
        self.staging_dir = self.base_dir / "downloads"
        self.incomplete_dir = self.staging_dir / ".incomplete"

        self.ajustes = Ajustes(self.base_dir)

        # Carpeta final: ahí queda SOLO el archivo de audio.
        self.download_dir = self.staging_dir
        self.aplicar_ajustes()
        self.log_arranque = self.base_dir / "slskd_arranque.log"

        # Último código HTTP que devolvió slskd al preguntarle si estaba vivo
        # (None = no hubo conexión) y si la API exige usuario y contraseña.
        self._ultimo_status = None
        self.requiere_login = False

        # Para reiniciar slskd solo la primera vez que se abre la app.
        self._primer_arranque_hecho = False
        self.base_url = slskd_url.rstrip("/")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

        # Permite muchas conexiones simultáneas a slskd (descargas en paralelo).
        adaptador = HTTPAdapter(pool_connections=20, pool_maxsize=50)
        self.session.mount("http://", adaptador)
        self.session.mount("https://", adaptador)

        self.slskd_process = None
        self.usuario = None
        self._busquedas_diagnosticadas = set()
        # slskd solo acepta crear UNA búsqueda a la vez (si no, responde HTTP 429).
        self._lock_busqueda = threading.Lock()
        # Resumen de lo que devolvió cada búsqueda (para explicar los "sin match").
        self._resumen_busqueda = {}
        # Todos los archivos de audio que devolvió cada búsqueda (sin filtrar).
        self._crudos_busqueda = {}
        self._dirs_slskd = None
        self._credenciales = {"username": None, "password": None}

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.incomplete_dir.mkdir(parents=True, exist_ok=True)

        self.limpiar_staging()

    # ========================================================
    # AJUSTES
    # ========================================================

    def aplicar_ajustes(self):
        """Pone en marcha los ajustes guardados (se puede llamar en cualquier momento)."""

        global SEGUNDOS_SIN_ARRANQUE, SEGUNDOS_SIN_PROGRESO, ESPERA_BUSQUEDA_SEG
        global CANDIDATOS_EN_CARRERA, PREFERIR_EXTENDED_MIX, MAX_SIMULTANEAS, FORMATO_NOMBRE

        a = self.ajustes

        SEGUNDOS_SIN_ARRANQUE = a.get("seg_sin_arranque")
        SEGUNDOS_SIN_PROGRESO = a.get("seg_sin_progreso")
        ESPERA_BUSQUEDA_SEG = a.get("espera_busqueda")
        CANDIDATOS_EN_CARRERA = a.get("candidatos_en_carrera")
        PREFERIR_EXTENDED_MIX = a.get("preferir_extended")
        MAX_SIMULTANEAS = a.get("max_simultaneas")
        FORMATO_NOMBRE = a.get("formato_nombre")

        try:
            import youtube

            youtube.YT_BITRATE_MP3 = str(a.get("youtube_bitrate"))

        except Exception:
            pass

        # Carpeta final
        elegida = str(a.get("carpeta_descargas") or "").strip()

        destino = Path(elegida).expanduser() if elegida else self.staging_dir

        try:
            destino.mkdir(parents=True, exist_ok=True)

        except Exception as e:
            print(f"[AJUSTES] No se pudo usar la carpeta «{destino}»: {e}. Se usa la predeterminada.")
            destino = self.staging_dir

        self.download_dir = destino

        # YouTube guarda directo en la carpeta final: se vuelve a crear con la carpeta nueva.
        self._yt = None

    # ========================================================
    # SLSKD - SERVIDOR
    # ========================================================

    def _estado_api(self):

        """
        Pregunta a slskd si está respondiendo.
        Devuelve (listo, descripcion): listo=True si la API contestó bien;
        descripcion explica el último problema (HTTP xxx o "sin conexión").
        Deja en self._ultimo_status el último código HTTP recibido.
        """

        ultimo = "sin conexión"

        self._ultimo_status = None

        for ruta in ("application", "server"):

            try:
                respuesta = self.session.get(
                    f"{self.base_url}/{ruta}",
                    timeout=3,
                )

                self._ultimo_status = respuesta.status_code

                if respuesta.ok:
                    return True, "ok"

                ultimo = f"HTTP {respuesta.status_code} en /{ruta}"

                # 401/403: slskd SÍ está funcionando, pero pide iniciar sesión.
                if respuesta.status_code in (401, 403):
                    return False, ultimo

            except Exception:
                pass

        return False, ultimo

    # ========================================================
    # AUTENTICACIÓN CON slskd (cuando la API responde HTTP 401)
    # ========================================================

    def _aplicar_token(self, respuesta):

        try:
            token = (respuesta.json() or {}).get("token")

        except Exception:
            token = None

        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            return True

        return False

    def _configuracion_slskd(self, solo_local=False):

        """Lee, de forma sencilla, los valores de slskd.yml (claves anidadas con puntos)."""

        rutas = [self.slskd_dir / "slskd.yml"]

        local = os.environ.get("LOCALAPPDATA")

        if local and not solo_local:
            rutas.append(Path(local) / "slskd" / "slskd.yml")

        if not solo_local:
            rutas.append(Path.home() / ".local" / "share" / "slskd" / "slskd.yml")

        datos = {}

        for ruta in rutas:

            try:

                if not ruta.exists():
                    continue

                pila = []

                for linea in ruta.read_text(encoding="utf-8", errors="replace").splitlines():

                    if not linea.strip() or linea.lstrip().startswith("#"):
                        continue

                    m = re.match(r"^(\s*)([A-Za-z0-9_\-\.]+):\s*(.*)$", linea)

                    if not m:
                        continue

                    sangria = len(m.group(1))

                    clave = m.group(2).lower()

                    valor = m.group(3).strip()

                    # Quitar comentarios al final de la línea y comillas
                    valor = re.split(r"\s+#", valor)[0].strip()

                    if len(valor) >= 2 and valor[0] == '"' and valor[-1] == '"':
                        try:
                            valor = json.loads(valor)
                        except Exception:
                            valor = valor[1:-1]
                    else:
                        valor = valor.strip("'\"")

                    while pila and pila[-1][0] >= sangria:
                        pila.pop()

                    ruta_clave = ".".join([k for _, k in pila] + [clave])

                    if valor:
                        datos.setdefault(ruta_clave, valor)

                    pila.append((sangria, clave))

            except Exception as e:
                print(f"[SLSKD] No se pudo leer {ruta}: {e}")

        return datos

    def _autenticar(self):

        """
        Intenta iniciar sesión en la API de slskd:
        1) con las credenciales que el usuario escribió en la app,
        2) con las de web.authentication de slskd.yml,
        3) con las que trae slskd por defecto (slskd / slskd),
        4) con una API key de slskd.yml.
        """

        config = self._configuracion_slskd()

        candidatos = []

        if self._credenciales.get("username") and self._credenciales.get("password"):
            candidatos.append(
                (self._credenciales["username"], self._credenciales["password"])
            )

        usuario_yml = config.get("web.authentication.username")
        clave_yml = config.get("web.authentication.password")

        if usuario_yml and clave_yml:
            candidatos.append((usuario_yml, clave_yml))

        candidatos.append(("slskd", "slskd"))

        vistos = set()

        for usuario, password in candidatos:

            if (usuario, password) in vistos:
                continue

            vistos.add((usuario, password))

            try:
                respuesta = requests.post(
                    f"{self.base_url}/session",
                    json={"username": usuario, "password": password},
                    timeout=8,
                )

            except Exception:
                continue

            if respuesta.ok and self._aplicar_token(respuesta):

                self.usuario = usuario

                print(f"[SLSKD] Sesión iniciada en slskd como '{usuario}'.")

                return True

        # API keys definidas en slskd.yml
        claves_api = [
            valor
            for ruta_clave, valor in config.items()
            if ruta_clave.startswith("web.authentication.api_keys.")
            and ruta_clave.endswith(".key")
        ]

        for clave in claves_api:

            self.session.headers["X-API-Key"] = clave

            listo, _ = self._estado_api()

            if listo:
                print("[SLSKD] Acceso a slskd con una API key de slskd.yml.")
                return True

            self.session.headers.pop("X-API-Key", None)

        return False

    def _resolver_autenticacion(self):

        """slskd respondió HTTP 401: intenta iniciar sesión. True si quedó listo."""

        print(
            "[SLSKD] slskd está funcionando, pero su API pide usuario y "
            "contraseña (HTTP 401). Intentando iniciar sesión..."
        )

        if self._autenticar():

            listo, _ = self._estado_api()

            if listo:
                self.requiere_login = False
                print("[SLSKD] Servidor listo.")
                return True

        self.requiere_login = True

        print("[SLSKD] No pude iniciar sesión en slskd automáticamente.")
        print(
            "         Opción 1: en la app, pulsa «Configurar Soulseek» y escribe el "
            "usuario y la contraseña de la web de slskd (por defecto: slskd / slskd)."
        )
        print(
            "         Opción 2: en slskd.yml pon  web: authentication: disabled: true"
        )

        return False

    def _leer_log_arranque(self, lineas=15):

        try:
            texto = self.log_arranque.read_text(encoding="utf-8", errors="replace")

            return "\n".join(texto.strip().splitlines()[-lineas:])

        except Exception:
            return ""

    def _cerrar_slskd_existente(self):

        """Cierra cualquier slskd.exe que esté abierto (por ejemplo, uno colgado de una sesión anterior)."""

        try:

            if platform.system() == "Windows":

                resultado = subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", "slskd.exe"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

            else:

                resultado = subprocess.run(
                    ["pkill", "-x", "slskd"],
                    capture_output=True,
                    text=True,
                )

            salida = (resultado.stdout or resultado.stderr or "").strip()

            if salida:
                print(f"[SLSKD] {salida}")

        except Exception as e:
            print(f"[SLSKD] No se pudo cerrar el slskd.exe anterior: {e}")
            return False

        # Dar tiempo a que Windows libere el puerto y el bloqueo de instancia única.
        time.sleep(2.0)

        self.slskd_process = None

        return True

    def _recuperar_instancia_existente(self):

        """
        Ya hay un slskd.exe abierto, pero no contesta en la API. Primero se le da
        un tiempo por si aún está arrancando; si sigue sin responder, se cierra y
        se inicia uno nuevo con la configuración de esta carpeta.
        """

        print(
            "[SLSKD] Ya hay un slskd.exe abierto pero no responde. "
            f"Esperando hasta {ESPERA_INSTANCIA_EXISTENTE_SEG}s por si todavía está arrancando..."
        )

        inicio = time.time()

        while time.time() - inicio < ESPERA_INSTANCIA_EXISTENTE_SEG:

            time.sleep(1.0)

            listo, motivo = self._estado_api()

            if listo:
                print("[SLSKD] El servidor ya está iniciado.")
                return True

            if self._ultimo_status in (401, 403):
                return self._resolver_autenticacion()

        print(
            "[SLSKD] Sigue sin responder: se cierra ese slskd.exe y se inicia "
            "uno nuevo con tu configuración."
        )

        self._cerrar_slskd_existente()

        return self.iniciar_servidor_slskd(_reintento=True)

    def iniciar_servidor_slskd(self, _reintento=False):

        # Opcional: empezar siempre desde cero con la configuración de esta carpeta.
        if (
            REINICIAR_SLSKD_AL_ABRIR
            and not self._primer_arranque_hecho
            and not _reintento
            and self.slskd_exe.exists()
        ):
            print("[SLSKD] Reiniciando slskd con tu configuración...")
            self._cerrar_slskd_existente()

        self._primer_arranque_hecho = True

        # Si el slskd.yml tenía carpetas mal escritas, se corrige y se reinicia slskd
        # para que use la configuración buena.
        if not _reintento and self.reparar_configuracion():
            self._cerrar_slskd_existente()

        # Primero comprobar si ya está funcionando.

        listo, motivo = self._estado_api()

        # Ya está funcionando, pero pide usuario y contraseña.
        if not listo and self._ultimo_status in (401, 403):
            return self._resolver_autenticacion()

        if listo:
            print("[SLSKD] El servidor ya está iniciado.")
            return True

        # Si lo arrancamos nosotros hace poco y sigue vivo, solo falta esperar.
        proceso_vivo = (
            self.slskd_process is not None
            and self.slskd_process.poll() is None
        )

        if not proceso_vivo:

            if not self.slskd_exe.exists():
                print("[SLSKD] No se encontró slskd.exe:")
                print(f"         {self.slskd_exe}")
                return False

            try:
                print("[SLSKD] Iniciando slskd.exe...")

                creationflags = 0

                if platform.system() == "Windows":
                    creationflags = subprocess.CREATE_NO_WINDOW

                # Lo que escriba slskd al arrancar queda en un archivo, para
                # poder ver por qué falla si no inicia.
                archivo_log = open(self.log_arranque, "w", encoding="utf-8", errors="replace")

                # Le indicamos a slskd dónde está su carpeta y su slskd.yml.
                entorno = dict(os.environ)
                entorno["SLSKD_APP_DIR"] = str(self.slskd_dir)
                entorno["SLSKD_CONFIG"] = str(self.slskd_dir / "slskd.yml")

                self.slskd_process = subprocess.Popen(
                    [str(self.slskd_exe)],
                    cwd=str(self.slskd_dir),
                    env=entorno,
                    stdout=archivo_log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )

            except Exception as e:
                print(f"[SLSKD] Error iniciando servidor: {e}")
                return False

        # Esperar a que responda la API.

        inicio = time.time()

        while time.time() - inicio < ESPERA_ARRANQUE_SLSKD_SEG:

            time.sleep(0.5)

            listo, motivo = self._estado_api()

            if listo:
                print("[SLSKD] Servidor listo.")
                return True

            # Arrancó, pero su API pide usuario y contraseña.
            if self._ultimo_status in (401, 403):
                return self._resolver_autenticacion()

            # ¿slskd se cerró solo? Entonces no tiene sentido seguir esperando.
            if self.slskd_process is not None:

                codigo = self.slskd_process.poll()

                if codigo is not None:

                    log = self._leer_log_arranque()

                    # slskd solo permite una instancia: ya había otro abierto.
                    if "already running" in log.lower() and not _reintento:
                        return self._recuperar_instancia_existente()

                    print(f"[SLSKD] slskd.exe se cerró al arrancar (código {codigo}).")

                    if log:
                        print("[SLSKD] Lo último que escribió slskd:")

                        for linea in log.splitlines():
                            print(f"         {linea}")

                    print(
                        "[SLSKD] Posibles causas: ya hay otro slskd.exe abierto "
                        "(míralo en el Administrador de tareas y ciérralo), "
                        "o un error en slskd.yml."
                    )

                    return False

            segundos = int(time.time() - inicio)

            if segundos and segundos % 5 == 0:
                print(
                    f"[SLSKD] Esperando servidor... ({segundos}s, "
                    f"último intento: {motivo})"
                )
                time.sleep(1.0)

        print(
            f"[SLSKD] Timeout: slskd no respondió en {ESPERA_ARRANQUE_SLSKD_SEG}s "
            f"(último intento: {motivo})."
        )

        log = self._leer_log_arranque()

        if log:
            print("[SLSKD] Lo último que escribió slskd:")

            for linea in log.splitlines():
                print(f"         {linea}")

        print(
            "[SLSKD] Revisa en el Administrador de tareas si hay un slskd.exe "
            "colgado de una sesión anterior y ciérralo."
        )

        return False

    # Alias para compatibilidad.

    def iniciar_slskd(self):
        return self.iniciar_servidor_slskd()

    # ========================================================
    # LOGIN / CREDENCIALES
    # ========================================================

    # ========================================================
    # CONFIGURACIÓN DE SOULSEEK (slskd.yml generado por la app)
    # ========================================================

    @staticmethod
    def _ruta_para_slskd(ruta, windows=None):
        """
        Ruta absoluta con los separadores del sistema (en Windows, con \\).
        slskd compara la ruta con su forma normalizada: si lleva «/» en Windows
        falla con «Only absolute paths may be specified».
        """

        if windows is None:
            windows = platform.system() == "Windows"

        texto = str(ruta)

        if windows:
            texto = texto.replace("/", "\\")
            while "\\\\" in texto[2:]:
                texto = texto[:2] + texto[2:].replace("\\\\", "\\")
            texto = texto.rstrip("\\") if len(texto) > 3 else texto
            return texto

        return os.path.normpath(texto)

    def reparar_configuracion(self, windows=None):
        """
        Corrige un slskd.yml (de una versión anterior de la app) cuyas carpetas
        de descarga usan «/» en Windows. Devuelve True si cambió algo.
        """

        if windows is None:
            windows = platform.system() == "Windows"

        if not windows:
            return False

        archivo = self.slskd_dir / "slskd.yml"

        try:
            if not archivo.exists():
                return False

            texto = archivo.read_text(encoding="utf-8", errors="replace")

        except Exception:
            return False

        cambiado = False
        lineas = []

        for linea in texto.splitlines():

            m = re.match(r"^(\s+)(downloads|incomplete):\s*(.*)$", linea)

            if m:

                valor = re.split(r"\s+#", m.group(3))[0].strip()
                comillas = valor[:1] if valor[:1] in "\"'" else ""
                limpio = valor.strip("\"'")

                if "/" in limpio and re.match(r"^[A-Za-z]:", limpio):

                    nuevo_valor = self._ruta_para_slskd(limpio, windows=True)

                    # En YAML entre comillas dobles hay que duplicar las barras.
                    if comillas == "'":
                        escrito = "'" + nuevo_valor + "'"
                    else:
                        escrito = json.dumps(nuevo_valor)

                    linea = f"{m.group(1)}{m.group(2)}: {escrito}"
                    cambiado = True

            lineas.append(linea)

        if cambiado:
            try:
                shutil.copy2(archivo, self.slskd_dir / "slskd.yml.bak")
                archivo.write_text("\n".join(lineas) + "\n", encoding="utf-8")
                print("[SLSKD] slskd.yml reparado: carpetas con separadores de Windows.")
            except Exception as e:
                print(f"[SLSKD] No se pudo reparar slskd.yml: {e}")
                return False

        return cambiado

    def usuario_soulseek_configurado(self):
        """Usuario de Soulseek que hay en slskd.yml ('' si no hay)."""
        return self._configuracion_slskd(solo_local=True).get("soulseek.username", "")

    def soulseek_configurado(self):
        """True si slskd.yml tiene usuario y contraseña de Soulseek."""
        datos = self._configuracion_slskd(solo_local=True)
        return bool(datos.get("soulseek.username") and datos.get("soulseek.password"))

    def escribir_configuracion(self, usuario, password):
        """
        Crea slskd.yml con las credenciales de Soulseek del usuario.
        Si ya había uno, se guarda una copia como slskd.yml.bak.
        """

        usuario = str(usuario or "").strip()
        password = str(password or "")

        if not usuario or not password:
            raise ValueError("Falta el usuario o la contraseña de Soulseek.")

        self.slskd_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.incomplete_dir.mkdir(parents=True, exist_ok=True)

        destino = self.slskd_dir / "slskd.yml"

        previo = self._configuracion_slskd(solo_local=True)

        usuario_web = previo.get("web.authentication.username") or "slskd"
        clave_web = previo.get("web.authentication.password") or secrets.token_urlsafe(12)

        if destino.exists():
            try:
                shutil.copy2(destino, self.slskd_dir / "slskd.yml.bak")
            except Exception as e:
                print(f"[SLSKD] No se pudo guardar copia de slskd.yml: {e}")

        def q(texto):
            # Una cadena JSON es también una cadena YAML válida entre comillas dobles.
            return json.dumps(str(texto))

        contenido = (
            "# Archivo generado por la app. Se puede editar a mano.\n"
            "web:\n"
            "  port: 5030\n"
            "  authentication:\n"
            f"    username: {q(usuario_web)}\n"
            f"    password: {q(clave_web)}\n"
            "directories:\n"
            f"  downloads: {q(self._ruta_para_slskd(self.staging_dir))}\n"
            f"  incomplete: {q(self._ruta_para_slskd(self.incomplete_dir))}\n"
            "soulseek:\n"
            f"  username: {q(usuario)}\n"
            f"  password: {q(password)}\n"
        )

        destino.write_text(contenido, encoding="utf-8")

        print(f"[SLSKD] slskd.yml escrito para el usuario de Soulseek '{usuario}'.")

    def esperar_login_soulseek(self, timeout=30):
        """
        Espera a que slskd inicie sesión en la red Soulseek.
        Devuelve (True, '') o (False, motivo).
        """

        inicio = time.time()
        ultimo = "sin respuesta del servidor"

        while time.time() - inicio < timeout:

            try:
                r = self.session.get(f"{self.base_url}/server", timeout=5)

                if r.ok:

                    datos = r.json() if r.content else {}

                    if isinstance(datos, dict):

                        estado = str(datos.get("state", ""))

                        if datos.get("isLoggedIn") is True or "loggedin" in estado.lower().replace(" ", ""):
                            return True, ""

                        ultimo = estado or "desconectado"

                elif r.status_code in (401, 403):
                    self._autenticar()
                    ultimo = "slskd pide usuario y contraseña"

            except Exception as e:
                ultimo = str(e)

            time.sleep(1.0)

        return False, ultimo

    def aplicar_configuracion_soulseek(self, usuario, password):
        """
        Guarda las credenciales, (re)inicia slskd y comprueba que Soulseek las acepta.
        Devuelve (True, '') o (False, mensaje para el usuario).
        """

        try:
            self.escribir_configuracion(usuario, password)
        except Exception as e:
            return False, f"No se pudo guardar la configuración: {e}"

        # slskd lee slskd.yml al arrancar: hay que reiniciarlo.
        self._cerrar_slskd_existente()
        self.requiere_login = False
        self._credenciales = {"username": None, "password": None}
        self.session.headers.pop("Authorization", None)

        if not self.iniciar_servidor_slskd(_reintento=True):
            return False, (
                "No se pudo iniciar slskd. Mira el archivo slskd_arranque.log "
                "para ver el motivo."
            )

        ok, motivo = self.esperar_login_soulseek()

        if not ok:
            return False, (
                "slskd arrancó, pero no pudo iniciar sesión en Soulseek "
                f"({motivo}). Revisa que el usuario y la contraseña sean correctos "
                "y que tengas internet. Si el usuario es nuevo, Soulseek lo crea "
                "solo; si ya existía, la contraseña tiene que ser la suya."
            )

        return True, ""

    def guardar_credenciales(self, usuario, password):

        usuario = str(usuario or "").strip()
        password = str(password or "")

        if not usuario or not password:
            return False

        self._credenciales["username"] = usuario
        self._credenciales["password"] = password

        try:
            respuesta = self.session.post(
                f"{self.base_url}/session",
                json={
                    "username": usuario,
                    "password": password,
                },
                timeout=10,
            )

            if respuesta.ok:
                self.usuario = usuario
                self._aplicar_token(respuesta)
                self.requiere_login = False
                print("[SLSKD] Credenciales aceptadas.")
                return True

            if respuesta.status_code in (401, 403):
                print("[SLSKD] Credenciales rechazadas.")
                return False

            # Si la autenticación web está desactivada,
            # /session puede no ser necesario.
            if respuesta.status_code in (404, 405):
                self.usuario = usuario
                print("[SLSKD] Login web no requerido.")
                return True

            print(f"[SLSKD] Error de login: HTTP {respuesta.status_code}")
            print(respuesta.text[:500])
            return False

        except Exception as e:
            print(f"[SLSKD] Error de login: {e}")
            return False

    # ========================================================
    # ESTADO DE RED
    # ========================================================

    def verificar_estado_red(self):

        try:
            respuesta = self.session.get(
                f"{self.base_url}/server",
                timeout=5,
            )

            if not respuesta.ok:
                return False

            datos = respuesta.json()

            if isinstance(datos, dict):

                for campo in ("state", "status", "connectionState"):

                    valor = datos.get(campo)

                    if valor is not None:

                        texto = str(valor).lower()

                        if any(
                            palabra in texto
                            for palabra in ("connected", "online")
                        ):
                            return True

                return True

            return bool(datos)

        except Exception as e:
            print(f"[SLSKD] Error verificando red: {e}")
            return False

    # ========================================================
    # EXPLORADOR
    # ========================================================

    def abrir_en_explorador(self):

        self.download_dir.mkdir(parents=True, exist_ok=True)

        try:
            if platform.system() == "Windows":
                os.startfile(str(self.download_dir))

            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(self.download_dir)])

            else:
                subprocess.Popen(["xdg-open", str(self.download_dir)])

        except Exception as e:
            print(f"[SLSKD] No se pudo abrir el explorador: {e}")

    # ========================================================
    # LIMPIAR HISTORIAL
    # ========================================================

    def limpiar_historial_slskd(self):

        try:
            respuesta = self.session.delete(
                f"{self.base_url}/transfers/downloads/all/completed",
                timeout=10,
            )

            print(
                "[SLSKD] Historial de descargas "
                f"limpiado ({respuesta.status_code})."
            )

        except Exception as e:
            print(f"[SLSKD] No se pudo limpiar historial: {e}")

    # ========================================================
    # NORMALIZACIÓN
    # ========================================================

    @staticmethod
    def _tokens(texto):

        texto = unicodedata.normalize("NFKD", str(texto or ""))

        texto = "".join(
            c for c in texto if not unicodedata.combining(c)
        )

        texto = texto.lower()

        texto = re.sub(r"[_\-–—.,;:()\[\]{}]+", " ", texto)

        return re.findall(r"[a-z0-9]+", texto)

    # ========================================================
    # ARTISTA
    # ========================================================

    @staticmethod
    def _opciones_artista(artista):

        artista = str(artista or "").strip()

        if not artista:
            return []

        opciones = []

        completo = SoulseekDownloader._tokens(artista)

        if completo:
            opciones.append(completo)

        partes = re.split(
            r"\s*(?:,|;|/|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*",
            artista,
            flags=re.IGNORECASE,
        )

        for parte in partes:

            tokens = SoulseekDownloader._tokens(parte)

            if tokens:
                opciones.append(tokens)

        resultado = []
        vistos = set()

        for opcion in opciones:

            clave = tuple(opcion)

            if clave in vistos:
                continue

            vistos.add(clave)
            resultado.append(opcion)

        return resultado

    # ========================================================
    # BITRATE
    # ========================================================

    @staticmethod
    def _bitrate_de(resultado):

        valor = resultado.get("bitrate")

        if valor is None:
            return 0.0

        try:
            return float(valor)

        except (TypeError, ValueError):
            return 0.0

    # ========================================================
    # EXTENDED
    # ========================================================

    def _es_extended(self, resultado):

        tokens = self._tokens(resultado.get("filename", ""))

        return "extended" in tokens and "mix" in tokens

    # ========================================================
    # EXTRAER ARCHIVOS
    #
    # - soporta listas y dicts
    # - soporta respuestas agrupadas por usuario
    # - soporta responses/files/results/items/data
    # - hereda username desde niveles superiores
    # - detecta archivos directamente
    # ========================================================

    def _extraer_archivos(self, datos):

        resultados = []
        vistos = set()

        CAMPOS_FILENAME = ("filename", "fileName", "path", "name")

        CAMPOS_CONTENEDORES = {
            "responses",
            "files",
            "lockedFiles",
            "results",
            "items",
            "data",
            "response",
            "file",
            "searchResponses",
            "searchResults",
        }

        CAMPOS_USUARIO = ("username", "user", "userName", "peer", "source")

        def extraer_usuario_directo(obj, actual):

            if not isinstance(obj, dict):
                return actual

            for campo in ("username", "user", "userName"):

                valor = obj.get(campo)

                if isinstance(valor, str) and valor.strip():
                    return valor.strip()

            # Algunas estructuras pueden tener:
            # "peer": {"username": "..."}

            for campo in ("peer", "source", "user"):

                valor = obj.get(campo)

                if isinstance(valor, dict):

                    for subcampo in ("username", "user", "userName"):

                        subvalor = valor.get(subcampo)

                        if isinstance(subvalor, str) and subvalor.strip():
                            return subvalor.strip()

            return actual

        def actualizar_contexto(obj, actual):

            contexto = dict(actual or {})

            if not isinstance(obj, dict):
                return contexto

            for campo in (
                "hasFreeUploadSlot",
                "freeSlot",
                "queueLength",
                "uploadSpeed",
            ):

                if campo in obj:
                    contexto[campo] = obj.get(campo)

            return contexto

        def parece_archivo(obj):

            if not isinstance(obj, dict):
                return False

            for campo in CAMPOS_FILENAME:

                valor = obj.get(campo)

                if not isinstance(valor, str):
                    continue

                valor = valor.strip()

                if not valor:
                    continue

                extension = obj.get("extension")

                if extension:

                    extension = str(extension).lower().strip()

                    if not extension.startswith("."):
                        extension = "." + extension

                    if extension in EXTENSIONES_AUDIO:
                        return True

                if Path(valor).suffix.lower() in EXTENSIONES_AUDIO:
                    return True

            return False

        def convertir_bitrate(archivo):

            bitrate = archivo.get("bitRate")

            if bitrate is None:
                bitrate = archivo.get("bitrate")

            if bitrate is None:
                bitrate = archivo.get("bit_rate")

            try:
                return float(bitrate or 0)

            except (TypeError, ValueError):
                return 0.0

        def procesar_archivo(archivo, usuario, contexto, locked=False):

            if not isinstance(archivo, dict):
                return

            filename = ""

            for campo in CAMPOS_FILENAME:

                valor = archivo.get(campo)

                if isinstance(valor, str) and valor.strip():
                    filename = valor.strip()
                    break

            if not filename:
                return

            extension = archivo.get("extension")

            if extension:

                extension = str(extension).lower().strip()

                if not extension.startswith("."):
                    extension = "." + extension

            else:
                extension = Path(filename).suffix.lower()

            if extension not in EXTENSIONES_AUDIO:
                return

            bitrate = convertir_bitrate(archivo)

            # El tamaño es necesario para QueueDownloadRequest.
            size = archivo.get("size")

            if size is None:
                size = archivo.get("fileSize")

            if size is None:
                size = archivo.get("length", 0)

            try:
                size = int(size or 0)

            except (TypeError, ValueError):
                size = 0

            clave = (
                str(usuario or "").lower().strip(),
                filename.replace("\\", "/").lower().strip(),
            )

            if clave in vistos:
                return

            vistos.add(clave)

            resultado = {
                "username": str(usuario or "").strip(),
                "filename": filename,
                "size": size,
                "bitrate": bitrate,
                "extension": extension,
                "hasFreeUploadSlot": contexto.get(
                    "hasFreeUploadSlot",
                    contexto.get("freeSlot", False),
                ),
                "queueLength": contexto.get("queueLength", 0),
                "uploadSpeed": contexto.get("uploadSpeed", 0),
                "locked": bool(locked),
            }

            resultados.append(resultado)

            if MOSTRAR_RESULTADOS_RAW:

                print(
                    "[SLSKD]   -> "
                    f"{filename} "
                    f"| ext={extension} "
                    f"| bitrate={bitrate:g}"
                    f" | size={size}"
                    f"{' | LOCKED' if locked else ''}"
                )

        def recorrer(obj, usuario_actual="", contexto_actual=None, locked=False):

            if obj is None:
                return

            contexto_actual = contexto_actual or {}

            if isinstance(obj, list):

                for elemento in obj:
                    recorrer(elemento, usuario_actual, contexto_actual, locked)

                return

            if not isinstance(obj, dict):
                return

            usuario = extraer_usuario_directo(obj, usuario_actual)
            contexto = actualizar_contexto(obj, contexto_actual)

            # CASO 1: el objeto mismo es un archivo

            if parece_archivo(obj):
                procesar_archivo(obj, usuario, contexto, locked)

            # CASO 2: recorrer contenedores conocidos

            for campo in CAMPOS_CONTENEDORES:

                if campo not in obj:
                    continue

                valor = obj.get(campo)

                if valor is None:
                    continue

                recorrer(
                    valor,
                    usuario,
                    contexto,
                    locked=(locked or campo == "lockedFiles"),
                )

            # CASO 3: respuestas agrupadas, donde el nombre del usuario
            # está en la CLAVE y no dentro del objeto.

            for clave, valor in obj.items():

                if clave in CAMPOS_CONTENEDORES:
                    continue

                if clave in CAMPOS_FILENAME:
                    continue

                if clave in CAMPOS_USUARIO:
                    continue

                usuario_para_hijo = usuario

                # Algunas representaciones usan el nombre completo del
                # archivo como clave: {"01-track.flac": {"size": 123}}.
                if (
                    isinstance(clave, str)
                    and clave.lower().endswith(tuple(EXTENSIONES_AUDIO))
                    and isinstance(valor, dict)
                ):
                    archivo_por_clave = dict(valor)
                    archivo_por_clave.setdefault("filename", clave)
                    procesar_archivo(
                        archivo_por_clave,
                        usuario,
                        contexto,
                        locked,
                    )
                    continue

                if isinstance(clave, str):

                    clave_lower = clave.lower()

                    claves_tecnicas = {
                        "status",
                        "state",
                        "id",
                        "count",
                        "total",
                        "complete",
                        "iscomplete",
                        "filecount",
                        "responsecount",
                    }

                    if (
                        not usuario_para_hijo
                        and clave_lower not in claves_tecnicas
                    ):

                        # Solo usar la clave como usuario si el valor
                        # contiene estructuras de respuesta/archivo.
                        if isinstance(valor, (dict, list)):
                            usuario_para_hijo = clave

                if isinstance(valor, (dict, list)):
                    recorrer(valor, usuario_para_hijo, contexto, locked)

        recorrer(datos)

        return resultados

    # ========================================================
    # OBTENER ESTADO / RESULTADOS
    # ========================================================

    def _obtener_estado_busqueda(self, search_id, incluir_respuestas=False):

        encoded_id = quote(str(search_id), safe="")

        params = None

        if incluir_respuestas:
            params = {"includeResponses": "true"}

        try:
            respuesta = self.session.get(
                f"{self.base_url}/searches/{encoded_id}",
                params=params,
                timeout=3,
            )

            if not respuesta.ok:
                return None

            datos = respuesta.json()

            if not isinstance(datos, dict):
                return None

            return datos

        except (requests.RequestException, ValueError):
            return None

        except Exception as e:
            print(f"[SLSKD] Error obteniendo estado: {e}")
            return None

    def obtener_resultados_busqueda(self, search_id, incluir_respuestas=True):

        encoded_id = quote(str(search_id), safe="")

        todos = []

        # Primero pedimos el estado con includeResponses=true.
        datos_estado = self._obtener_estado_busqueda(
            search_id,
            incluir_respuestas=incluir_respuestas,
        )

        datos_a_procesar = []

        if datos_estado is not None:
            datos_a_procesar.append(datos_estado)

        # Como respaldo, consultar /responses.
        try:
            respuesta = self.session.get(
                f"{self.base_url}/searches/{encoded_id}/responses",
                timeout=3,
            )

            if respuesta.ok:
                datos_a_procesar.append(respuesta.json())

        except Exception:
            pass

        # Diagnóstico solamente una vez por búsqueda.
        if search_id not in self._busquedas_diagnosticadas:

            print("[SLSKD] Estado inicial de búsqueda:")

            if datos_estado is not None:

                print(
                    "         "
                    f"state={datos_estado.get('state')} | "
                    f"isComplete={datos_estado.get('isComplete')} | "
                    f"fileCount={datos_estado.get('fileCount')} | "
                    f"responseCount={datos_estado.get('responseCount')}"
                )

                respuestas = datos_estado.get("responses")

                print(
                    "         "
                    f"responses="
                    f"{len(respuestas) if isinstance(respuestas, list) else type(respuestas).__name__}"
                )

            self._busquedas_diagnosticadas.add(search_id)

        for datos in datos_a_procesar:

            resultados = self._extraer_archivos(datos)

            if resultados:
                todos.extend(resultados)

        return self._deduplicar_resultados(todos)

    # ========================================================
    # DEDUPLICAR
    # ========================================================

    def _deduplicar_resultados(self, resultados):

        vistos = set()
        salida = []

        for resultado in resultados:

            usuario = str(resultado.get("username", "")).lower().strip()

            filename = (
                str(resultado.get("filename", ""))
                .replace("\\", "/")
                .lower()
                .strip()
            )

            clave = (usuario, filename)

            if clave in vistos:
                continue

            vistos.add(clave)
            salida.append(resultado)

        return salida

    # ========================================================
    # BÚSQUEDA COMPLETA
    # ========================================================

    def _busqueda_completa(self, search_id):

        datos = self._obtener_estado_busqueda(
            search_id,
            incluir_respuestas=False,
        )

        if not isinstance(datos, dict):
            return False

        if "isComplete" in datos:
            return bool(datos.get("isComplete"))

        if "complete" in datos:
            return bool(datos.get("complete"))

        estado = str(
            datos.get("status", datos.get("state", ""))
        ).lower()

        return estado in {"complete", "completed", "finished", "done"}

    # ========================================================
    # VERSIÓN DEL TÍTULO
    # ========================================================

    # Palabras que describen una "versión" del tema sin cambiar de qué tema se
    # trata. Ojo: "remix", "dub", "instrumental", "live", etc. NO están en la
    # lista porque son otra versión distinta del tema.
    _PALABRAS_VERSION = {
        "radio",
        "edit",
        "mix",
        "version",
        "original",
        "extended",
        "club",
        "album",
        "single",
        "remaster",
        "remastered",
        "cut",
    }

    @staticmethod
    def _separar_version(titulo):

        """
        Separa el título de Spotify en (título_base, palabras_de_versión).

        "Tema - Radio Edit"      -> ("Tema", ["radio", "edit"])
        "Tema (Extended Mix)"    -> ("Tema", ["extended", "mix"])
        "Tema - Artista Remix"   -> ("Tema - Artista Remix", [])   (no se toca)
        """

        base = str(titulo or "").strip()

        version = []

        palabras = SoulseekDownloader._PALABRAS_VERSION

        def es_version(texto):

            tokens = SoulseekDownloader._tokens(texto)

            if not tokens:
                return False

            if not any(tok in palabras for tok in tokens):
                return False

            return all(
                tok in palabras or (tok.isdigit() and len(tok) == 4)
                for tok in tokens
            )

        cambio = True

        while cambio:

            cambio = False

            # Entre paréntesis o corchetes: "Tema (Radio Edit)"
            for m in re.finditer(r"\s*[\(\[]([^\)\]]*)[\)\]]", base):

                if es_version(m.group(1)):

                    version += SoulseekDownloader._tokens(m.group(1))

                    base = (base[: m.start()] + base[m.end():]).strip()

                    cambio = True

                    break

            if cambio:
                continue

            # Después de un guion: "Tema - Radio Edit"
            partes = re.split(r"\s+[-–—]\s+", base)

            if len(partes) > 1 and es_version(partes[-1]):

                version += SoulseekDownloader._tokens(partes[-1])

                base = " - ".join(partes[:-1]).strip()

                cambio = True

        if not base:
            return str(titulo or "").strip(), []

        return base, version

    # ========================================================
    # RANKEO
    # ========================================================

    def rankear_resultados(self, resultados, artista, titulo, limite=MAX_CANDIDATOS):

        titulo_tokens = set(self._tokens(titulo))
        opciones_artista = self._opciones_artista(artista)

        if not titulo_tokens:
            return []

        if not opciones_artista:
            return []

        base_titulo, version_tokens = self._separar_version(titulo)

        base_tokens = set(self._tokens(base_titulo))

        def filtrar(tokens_requeridos):

            salida = []

            for resultado in resultados:

                filename = str(resultado.get("filename", ""))

                extension = str(resultado.get("extension", "")).lower()

                if extension not in EXTENSIONES_AUDIO:
                    continue

                # MATCH CONTRA TODO EL PATH
                tokens_path = set(self._tokens(filename))

                # TÍTULO
                if not tokens_requeridos.issubset(tokens_path):
                    continue

                # ARTISTA
                artista_encontrado = False

                for opcion in opciones_artista:

                    if set(opcion).issubset(tokens_path):
                        artista_encontrado = True
                        break

                if not artista_encontrado:
                    continue

                # ARCHIVO BLOQUEADO
                if resultado.get("locked", False):
                    continue

                salida.append(resultado)

            return salida

        # 1) Título completo (por ejemplo "Tema Radio Edit").
        validos = filtrar(titulo_tokens)

        # 2) Si no hay nada, se acepta solo el título base ("Tema"): muchos
        #    archivos no dicen "Radio Edit" pero son el mismo tema.
        if not validos and base_tokens and base_tokens != titulo_tokens:

            validos = filtrar(base_tokens)

            for resultado in validos:
                resultado["coincidencia"] = (
                    "solo por título base "
                    f"(el archivo no indica '{' '.join(version_tokens)}')"
                )

        # ORDEN
        #
        # 1. Extended MP3 320
        # 2. MP3 320
        # 3. Extended otros formatos
        # 4. Otros formatos
        #
        # NO hay sistema de puntos.

        def clave(resultado):

            extension = str(resultado.get("extension", "")).lower()

            bitrate = self._bitrate_de(resultado)

            extended = self._es_extended(resultado)

            if (
                extension == ".mp3"
                and bitrate == BITRATE_MINIMO_MP3
                and extended
            ):
                categoria = 0

            elif extension == ".mp3" and bitrate == BITRATE_MINIMO_MP3:
                categoria = 1

            elif extended:
                categoria = 2

            else:
                categoria = 3

            free_slot = (
                0 if resultado.get("hasFreeUploadSlot", False) else 1
            )

            try:
                queue = float(resultado.get("queueLength", 0) or 0)

            except (TypeError, ValueError):
                queue = 999999

            try:
                speed = float(resultado.get("uploadSpeed", 0) or 0)

            except (TypeError, ValueError):
                speed = 0

            # Dentro de la misma calidad, se prefieren los archivos que
            # dicen la versión pedida (por ejemplo "radio edit").
            tokens_archivo = set(self._tokens(resultado.get("filename", "")))

            version_ok = (
                1
                if version_tokens
                and not set(version_tokens).issubset(tokens_archivo)
                else 0
            )

            return (categoria, version_ok, free_slot, queue, -speed)

        validos.sort(key=clave)

        return validos[:limite] if limite else validos

    # ========================================================
    # ESPERAR RESULTADOS
    # ========================================================

    def _detener_busqueda(self, search_id):

        # slskd solo entrega las respuestas cuando la búsqueda termina. Si se
        # alarga demasiado, la detenemos para poder leer lo que ya llegó.

        encoded_id = quote(str(search_id), safe="")

        try:
            respuesta = self.session.put(
                f"{self.base_url}/searches/{encoded_id}",
                timeout=5,
            )

            return respuesta.ok

        except Exception:
            return False

    def esperar_resultados(self, search_id, artista, titulo, control=None):

        inicio = time.time()

        # slskd pone las búsquedas en una fila interna (estado "Queued") y
        # solo ejecuta algunas a la vez. Los tiempos cuentan desde que la
        # búsqueda ARRANCA de verdad, no mientras hace fila.
        inicio_activo = None

        # Último momento en que llegaron respuestas nuevas.
        ultimo_cambio = None
        firma_anterior = None

        detenida = False
        inicio_detencion = None

        mejor = []

        ultimo_crudos = -1
        ultimo_validos = -1
        ultimo_file_count = -1
        ultimo_response_count = -1
        ultimo_estado = None

        while True:

            ahora = time.time()

            if inicio_activo is None and ahora - inicio >= ESPERA_COLA_SEG:

                print(
                    "[SLSKD] La búsqueda no salió de la fila de slskd "
                    f"en {ESPERA_COLA_SEG}s: {artista} - {titulo}"
                )

                break

            if detenida and ahora - inicio_detencion >= SEGUNDOS_GRACIA_DETENCION:
                break

            if control and control.cancelar.is_set():
                return []

            # IMPORTANTE: pedimos el estado CON respuestas.
            datos_estado = self._obtener_estado_busqueda(
                search_id,
                incluir_respuestas=True,
            )

            crudos = []
            is_complete = False
            file_count = 0
            response_count = 0
            estado = ""

            if isinstance(datos_estado, dict):

                is_complete = bool(datos_estado.get("isComplete", False))

                file_count = datos_estado.get("fileCount", 0) or 0

                response_count = datos_estado.get("responseCount", 0) or 0

                estado = str(
                    datos_estado.get(
                        "state",
                        datos_estado.get("status", ""),
                    )
                )

            en_cola = "queued" in estado.lower()

            # La búsqueda salió de la fila: desde ahora corre el reloj.
            if estado and not en_cola and inicio_activo is None:
                inicio_activo = time.time()
                ultimo_cambio = inicio_activo

            if en_cola:

                if estado != ultimo_estado:
                    print(
                        "[SLSKD] Búsqueda en fila de slskd, esperando turno: "
                        f"{artista} - {titulo}"
                    )

                ultimo_estado = estado

                time.sleep(INTERVALO_BUSQUEDA_SEG)

                continue

            ultimo_estado = estado

            # ¿Llegaron respuestas nuevas?
            firma = (file_count, response_count)

            if firma != firma_anterior:
                firma_anterior = firma
                ultimo_cambio = time.time()

            if isinstance(datos_estado, dict):
                crudos = self._extraer_archivos(datos_estado)

            # Si includeResponses todavía no contiene los archivos,
            # intentamos el endpoint específico como respaldo.
            if not crudos:

                crudos = self.obtener_resultados_busqueda(
                    search_id,
                    incluir_respuestas=False,
                )

            if crudos:
                self._crudos_busqueda[search_id] = crudos

            validos = self.rankear_resultados(crudos, artista, titulo)

            segundos_activo = (
                time.time() - inicio_activo if inicio_activo else 0
            )

            # Resumen para poder explicar un "sin match".
            self._resumen_busqueda[search_id] = {
                "archivos": file_count,
                "respuestas": response_count,
                "audio": len(crudos),
                "estado": estado,
            }

            if (
                len(crudos) != ultimo_crudos
                or len(validos) != ultimo_validos
                or file_count != ultimo_file_count
                or response_count != ultimo_response_count
            ):

                print(
                    "[SLSKD] Búsqueda "
                    f"({artista} - {titulo}): "
                    f"{file_count} archivos encontrados, "
                    f"{response_count} respuestas, "
                    f"{len(crudos)} archivos de audio extraídos, "
                    f"{len(validos)} matches "
                    f"({segundos_activo:.1f}s) "
                    f"[{estado or 'InProgress'}]"
                )

                ultimo_crudos = len(crudos)
                ultimo_validos = len(validos)
                ultimo_file_count = file_count
                ultimo_response_count = response_count

            if validos:

                mejor = validos

                primero = validos[0]

                # Si aparece Extended MP3 320, ya tenemos el
                # resultado de máxima prioridad.
                if (
                    primero.get("extension") == ".mp3"
                    and self._bitrate_de(primero) == BITRATE_MINIMO_MP3
                    and self._es_extended(primero)
                ):

                    print(
                        "[SLSKD] Match óptimo encontrado: "
                        f"{primero.get('filename')}"
                    )

                    return validos

                # Si ya tenemos un match válido, damos unos segundos
                # para permitir que aparezca uno mejor.
                if segundos_activo >= SEGUNDOS_ESPERAR_MEJOR_MATCH:
                    return validos

            # Si slskd terminó, ya no tiene sentido seguir esperando.
            if is_complete:

                print(
                    "[SLSKD] Búsqueda completada: "
                    f"{file_count} archivos / "
                    f"{response_count} respuestas."
                )

                return mejor

            # ¿Hay que detener la búsqueda para leer los resultados?
            if inicio_activo is not None and not detenida:

                silencio = time.time() - ultimo_cambio if ultimo_cambio else 0

                motivo_stop = None

                if segundos_activo >= ESPERA_BUSQUEDA_SEG:
                    motivo_stop = f"{int(segundos_activo)}s de búsqueda"

                elif response_count > 0 and silencio >= SEGUNDOS_SILENCIO_BUSQUEDA:
                    motivo_stop = f"{int(silencio)}s sin respuestas nuevas"

                if motivo_stop:

                    print(
                        "[SLSKD] Deteniendo la búsqueda para leer los "
                        f"resultados ({motivo_stop}): {artista} - {titulo}"
                    )

                    self._detener_busqueda(search_id)

                    detenida = True

                    inicio_detencion = time.time()

            time.sleep(INTERVALO_BUSQUEDA_SEG)

        # Último intento: consulta final de las respuestas.
        try:
            finales = self.obtener_resultados_busqueda(
                search_id,
                incluir_respuestas=True,
            )

            finales_validos = self.rankear_resultados(
                finales,
                artista,
                titulo,
            )

            if finales:

                self._crudos_busqueda[search_id] = finales

                resumen = self._resumen_busqueda.setdefault(search_id, {})

                resumen["audio"] = max(resumen.get("audio", 0), len(finales))

            if finales_validos:
                mejor = finales_validos

        except Exception as e:
            print(f"[SLSKD] Error en consulta final de resultados: {e}")

        print(
            "[SLSKD] Se alcanzó el tiempo máximo de búsqueda "
            f"({artista} - {titulo})."
        )

        return mejor

    # ========================================================
    # CREAR BÚSQUEDA
    # ========================================================

    def iniciar_busqueda(self, artista, titulo, control=None):

        return self.iniciar_busqueda_texto(f"{artista} - {titulo}", control)

    def iniciar_busqueda_texto(self, consulta, control=None):

        payload = {
            "searchText": consulta,
            "searchTimeout": BUSQUEDA_INACTIVIDAD_MS,
        }

        avisado = False

        for intento in range(1, 31):

            if control and control.cancelar.is_set():
                return None

            try:
                # slskd solo permite crear UNA búsqueda a la vez. Con el
                # candado, nuestras canciones hacen fila para crearla; una vez
                # creada, la búsqueda sigue en paralelo con las demás.
                with self._lock_busqueda:
                    respuesta = self.session.post(
                        f"{self.base_url}/searches",
                        json=payload,
                        timeout=10,
                    )

                # 429 = slskd está ocupado creando otra búsqueda: esperar y reintentar.
                if respuesta.status_code == 429:

                    if not avisado:
                        print(f"[SLSKD] slskd ocupado, reintentando búsqueda: {consulta}")
                        avisado = True

                    time.sleep(min(0.4 * intento, 2.0))
                    continue

                if not respuesta.ok:

                    print(
                        "[SLSKD] Error creando búsqueda:"
                        f" HTTP {respuesta.status_code}"
                    )

                    print(respuesta.text[:500])

                    return None

                datos = respuesta.json()

                search_id = None

                if isinstance(datos, dict):
                    search_id = datos.get("id") or datos.get("searchId")

                elif isinstance(datos, str):
                    search_id = datos

                if not search_id:

                    print("[SLSKD] La API no devolvió un ID de búsqueda.")
                    print(f"[SLSKD] Respuesta: {datos}")

                    return None

                print(f"[SLSKD] Búsqueda creada: {consulta}")
                print(f"[SLSKD] Search ID: {search_id}")

                return search_id

            except Exception as e:
                print(f"[SLSKD] Error creando búsqueda: {e}")
                return None

        print(f"[SLSKD] No se pudo crear la búsqueda tras varios intentos: {consulta}")

        return None

    # ========================================================
    # BÚSQUEDA MANUAL (texto libre, resultados sin filtrar)
    # ========================================================

    def esperar_crudos(self, search_id, etiqueta, control=None, progreso=None):

        inicio = time.time()

        inicio_activo = None
        ultimo_cambio = None
        firma_anterior = None

        detenida = False
        inicio_detencion = None

        while True:

            ahora = time.time()

            if inicio_activo is None and ahora - inicio >= ESPERA_COLA_SEG:
                break

            if detenida and ahora - inicio_detencion >= SEGUNDOS_GRACIA_DETENCION:
                break

            if control and control.cancelar.is_set():
                return []

            datos = self._obtener_estado_busqueda(
                search_id,
                incluir_respuestas=True,
            )

            estado = ""
            is_complete = False
            file_count = 0
            response_count = 0

            if isinstance(datos, dict):

                is_complete = bool(datos.get("isComplete", False))

                file_count = datos.get("fileCount", 0) or 0

                response_count = datos.get("responseCount", 0) or 0

                estado = str(datos.get("state", datos.get("status", "")))

            en_cola = "queued" in estado.lower()

            if estado and not en_cola and inicio_activo is None:
                inicio_activo = time.time()
                ultimo_cambio = inicio_activo

            if en_cola:

                if progreso:
                    progreso("En la fila de slskd, esperando turno...")

                time.sleep(INTERVALO_BUSQUEDA_SEG)

                continue

            firma = (file_count, response_count)

            if firma != firma_anterior:
                firma_anterior = firma
                ultimo_cambio = time.time()

            segundos_activo = time.time() - inicio_activo if inicio_activo else 0

            if progreso:
                progreso(
                    f"Buscando: {response_count} respuestas, "
                    f"{file_count} archivos ({segundos_activo:.0f}s)"
                )

            if is_complete:
                return self._deduplicar_resultados(self._extraer_archivos(datos))

            if inicio_activo is not None and not detenida:

                silencio = time.time() - ultimo_cambio if ultimo_cambio else 0

                if segundos_activo >= ESPERA_BUSQUEDA_SEG or (
                    response_count > 0 and silencio >= SEGUNDOS_SILENCIO_BUSQUEDA
                ):

                    print(
                        "[SLSKD] Deteniendo la búsqueda manual para leer "
                        f"los resultados: {etiqueta}"
                    )

                    self._detener_busqueda(search_id)

                    detenida = True

                    inicio_detencion = time.time()

            time.sleep(INTERVALO_BUSQUEDA_SEG)

        return self.obtener_resultados_busqueda(
            search_id,
            incluir_respuestas=True,
        )

    def buscar_manual(self, consulta, control=None, progreso=None):

        """
        Busca en slskd con un texto libre y devuelve TODOS los archivos de
        audio que respondieron, sin aplicar el filtro de artista/título.
        Devuelve None si no se pudo crear la búsqueda.
        """

        consulta = str(consulta or "").strip()

        if not consulta:
            return []

        if progreso:
            progreso("Creando la búsqueda...")

        search_id = self.iniciar_busqueda_texto(consulta, control)

        if not search_id:
            return None

        crudos = self.esperar_crudos(search_id, consulta, control, progreso)

        self._crudos_busqueda.pop(search_id, None)

        print(f"[SLSKD] Búsqueda manual '{consulta}': {len(crudos)} archivos de audio.")

        return crudos

    # ========================================================
    # DESCARGA
    #
    # slskd espera una LISTA:
    #
    # [{"filename": "...", "size": 123456}]
    # ========================================================

    def iniciar_descarga(self, resultado):

        username = str(resultado.get("username", "")).strip()

        filename = str(resultado.get("filename", "")).strip()

        if not username or not filename:
            print("[SLSKD] Resultado inválido para descarga.")
            return None

        encoded_user = quote(username, safe="")

        url = f"{self.base_url}/transfers/downloads/{encoded_user}"

        size = resultado.get("size", 0)

        try:
            size = int(size or 0)

        except (TypeError, ValueError):
            size = 0

        # QueueDownloadRequest[]
        payload = [
            {
                "filename": filename,
                "size": size,
            }
        ]

        try:
            print("[SLSKD] Encolando descarga...")
            print(f"         Usuario: {username}")
            print(f"         Archivo: {filename}")
            print(f"         Size: {size}")

            respuesta = self.session.post(url, json=payload, timeout=15)

            # Si slskd pide esperar (429), reintentamos unas veces.
            for _ in range(8):

                if respuesta.status_code != 429:
                    break

                time.sleep(1.0)

                respuesta = self.session.post(url, json=payload, timeout=15)

            if not respuesta.ok:

                print(
                    "[SLSKD] Error iniciando descarga:"
                    f" HTTP {respuesta.status_code}"
                )

                print(respuesta.text[:1500])

                return None

            datos = None

            if respuesta.content:

                try:
                    datos = respuesta.json()

                except Exception:
                    datos = None

            print("[SLSKD] Descarga encolada correctamente.")

            return datos if datos is not None else True

        except Exception as e:
            print(f"[SLSKD] Error de descarga: {e}")
            return None

    # ========================================================
    # TRANSFERS
    # ========================================================

    def obtener_descargas(self):

        try:
            respuesta = self.session.get(
                f"{self.base_url}/transfers/downloads",
                timeout=5,
            )

            if not respuesta.ok:
                return []

            datos = respuesta.json()

            if isinstance(datos, list):
                return datos

            if isinstance(datos, dict):

                # Estructuras conocidas.
                for campo in (
                    "downloads",
                    "items",
                    "results",
                    "transfers",
                    "data",
                ):

                    valor = datos.get(campo)

                    if isinstance(valor, list):
                        return valor

                # slskd puede devolver transferencias agrupadas por username.
                encontrados = []

                for clave, valor in datos.items():

                    if isinstance(valor, list):

                        for item in valor:

                            if isinstance(item, dict):

                                copia = dict(item)

                                if not copia.get("username"):
                                    copia["username"] = clave

                                encontrados.append(copia)

                    elif isinstance(valor, dict):

                        copia = dict(valor)

                        if not copia.get("username"):
                            copia["username"] = clave

                        encontrados.append(copia)

                return encontrados

            return []

        except Exception as e:
            print(f"[SLSKD] Error obteniendo transfers: {e}")
            return []

    # ========================================================
    # BUSCAR TRANSFER
    # ========================================================

    def _buscar_transfer(self, username, filename, solo_usuario=False):

        filename_norm = (
            str(filename or "").replace("\\", "/").lower().strip()
        )

        username_norm = str(username or "").lower().strip()

        candidatos = []

        if username:

            encoded_user = quote(str(username), safe="")

            try:
                respuesta = self.session.get(
                    f"{self.base_url}/transfers/downloads/{encoded_user}",
                    timeout=4,
                )

                if respuesta.ok:
                    candidatos.append(respuesta.json())

            except Exception:
                pass

        try:
            candidatos.append(self.obtener_descargas())

        except Exception:
            pass

        encontrados_exactos = []
        encontrados_nombre = []

        def recorrer(obj, usuario_heredado="", clave_nombre=""):

            if isinstance(obj, list):

                for item in obj:
                    recorrer(item, usuario_heredado, "")

                return

            if not isinstance(obj, dict):
                return

            usuario = str(
                obj.get("username", obj.get("user", usuario_heredado))
                or usuario_heredado
            )

            nombre = str(
                obj.get(
                    "filename",
                    obj.get(
                        "remoteFilename",
                        obj.get("fileName", clave_nombre),
                    ),
                )
                or ""
            )

            nombre_norm = nombre.replace("\\", "/").lower().strip()

            coincide_nombre = bool(
                filename_norm
                and (
                    filename_norm == nombre_norm
                    or filename_norm in nombre_norm
                    or nombre_norm in filename_norm
                )
            )

            coincide_usuario = (
                not username_norm
                or not usuario
                or username_norm == usuario.lower().strip()
            )

            parece_transfer = (
                "percentComplete" in obj
                or "bytesTransferred" in obj
                or "bytesRemaining" in obj
                or "state" in obj
                or "status" in obj
                or "direction" in obj
                or "token" in obj
                or isinstance(obj.get("transfer"), dict)
            )

            if coincide_nombre and parece_transfer:

                copia = dict(obj)

                if not copia.get("filename") and nombre:
                    copia["filename"] = nombre

                if not copia.get("username") and usuario:
                    copia["username"] = usuario

                if coincide_usuario:
                    encontrados_exactos.append(copia)

                else:
                    encontrados_nombre.append(copia)

            for clave, valor in obj.items():

                if not isinstance(valor, (dict, list)):
                    continue

                usuario_hijo = usuario
                clave_nombre_hijo = ""

                if isinstance(clave, str):

                    clave_norm = clave.replace("\\", "/").lower().strip()

                    if (
                        not usuario_hijo
                        and clave_norm.endswith(tuple(EXTENSIONES_AUDIO))
                    ):
                        clave_nombre_hijo = clave

                    elif clave_norm == filename_norm:
                        clave_nombre_hijo = clave

                recorrer(valor, usuario_hijo, clave_nombre_hijo)

        for datos in candidatos:
            recorrer(datos)

        # Con solo_usuario=True (carrera de candidatos) NO se acepta la
        # transferencia de otro usuario aunque el nombre del archivo coincida,
        # para no confundir dos candidatos que tienen el mismo archivo.
        if solo_usuario:
            encontrados_nombre = []

        posibles = (
            encontrados_exactos
            if encontrados_exactos
            else encontrados_nombre
        )

        if not posibles:
            return None

        # Puede haber más de un registro del mismo archivo. Elegimos el que
        # represente el estado más avanzado, no simplemente el primero.
        def clave_transfer(transfer):

            info = self._extraer_progreso_transfer(transfer)

            estado = str(info.get("estado", "")).lower()

            progreso = float(info.get("progreso", 0) or 0)

            prioridad_estado = 0

            if any(x in estado for x in ("complete", "succeeded", "success")):
                prioridad_estado = 3

            elif any(
                x in estado
                for x in ("downloading", "transferring", "active")
            ):
                prioridad_estado = 2

            elif any(x in estado for x in ("queued", "waiting", "pending")):
                prioridad_estado = 1

            return (progreso, prioridad_estado)

        elegido = max(posibles, key=clave_transfer)

        if encontrados_nombre and not encontrados_exactos:
            print(
                "[SLSKD] Transfer encontrada por nombre; "
                "el usuario no coincidió exactamente."
            )

        return elegido

    # ========================================================
    # PROGRESO DE TRANSFER
    # ========================================================

    def _extraer_progreso_transfer(self, transfer):

        if not isinstance(transfer, dict):
            return {
                "estado": "queued",
                "progreso": 0,
            }

        inner = transfer.get("transfer")
        inner = inner if isinstance(inner, dict) else {}

        # slskd puede entregar los datos directamente en el Transfer o
        # envolverlos dentro de {"transfer": {...}}. No debemos elegir
        # ciegamente el interno porque podría contener un 0 mientras el
        # objeto externo ya tiene el porcentaje actualizado.
        fuentes = [inner, transfer]

        estados = []
        progresos = []

        for datos in fuentes:

            estado = datos.get("state", datos.get("status"))

            if estado not in (None, ""):
                estados.append(str(estado))

            for campo in ("percentComplete", "percent", "percentage", "progress"):

                valor = datos.get(campo)

                if valor is not None:

                    try:
                        progresos.append(float(valor))

                    except (TypeError, ValueError):
                        pass

        # También podemos calcular el porcentaje desde bytes.
        for datos in fuentes:

            try:
                total = float(datos.get("size", 0) or 0)
                transferido = float(datos.get("bytesTransferred", 0) or 0)

                if total > 0:
                    progresos.append(transferido / total * 100)

            except (TypeError, ValueError):
                pass

        progreso = max(progresos) if progresos else 0.0

        # Preferir un estado activo/completado si está presente en alguna
        # de las dos capas.
        estado = "queued"

        if estados:

            estado = estados[0]

            for candidato in estados:

                candidato_lower = candidato.lower()

                if any(
                    x in candidato_lower
                    for x in ("complete", "succeeded", "success")
                ):
                    estado = candidato
                    break

                if any(
                    x in candidato_lower
                    for x in ("downloading", "transferring", "active")
                ):
                    estado = candidato

        progreso = max(0.0, min(100.0, progreso))

        # Mensaje de error que informa slskd (por ejemplo, el motivo por el
        # que el otro usuario rechazó la descarga).
        motivo = ""

        for datos in fuentes:

            for campo in ("exception", "error", "message"):

                valor = datos.get(campo)

                if isinstance(valor, str) and valor.strip():
                    motivo = valor.strip()
                    break

            if motivo:
                break

        return {
            "estado": estado,
            "progreso": progreso,
            "transfer": transfer,
            "motivo": motivo,
        }

    # ========================================================
    # CANCELAR TRANSFER
    # ========================================================

    def cancelar_transfer(self, transfer, username):

        if not isinstance(transfer, dict):
            return False

        transfer_id = transfer.get("id")

        if not transfer_id:

            nested = transfer.get("transfer")

            if isinstance(nested, dict):
                transfer_id = nested.get("id")

        if not transfer_id:
            return False

        encoded_user = quote(str(username), safe="")
        encoded_id = quote(str(transfer_id), safe="")

        try:
            respuesta = self.session.delete(
                f"{self.base_url}/transfers/downloads/"
                f"{encoded_user}/{encoded_id}",
                timeout=5,
            )

            return respuesta.ok

        except Exception:
            return False

    # ========================================================
    # CALIDAD / ESTADO DE TRANSFERENCIAS
    # ========================================================

    def _categoria_resultado(self, resultado):

        extension = str(resultado.get("extension", "")).lower()

        bitrate = self._bitrate_de(resultado)

        extended = self._es_extended(resultado)

        if extension == ".mp3" and bitrate == BITRATE_MINIMO_MP3 and extended:
            return 0

        if extension == ".mp3" and bitrate == BITRATE_MINIMO_MP3:
            return 1

        if extended:
            return 2

        return 3

    @staticmethod
    def _transfer_fallo(estado):

        # slskd usa estados como "Completed, Succeeded", pero también
        # "Completed, Errored", "Completed, Rejected", "Completed, TimedOut"
        # o "Completed, Cancelled". Todos empiezan con "Completed", así que
        # primero se detecta el fallo.

        estado = str(estado or "").lower()

        return any(
            palabra in estado
            for palabra in (
                "error",
                "failed",
                "cancelled",
                "canceled",
                "rejected",
                "timedout",
                "timed out",
                "aborted",
            )
        )

    @staticmethod
    def _explicar_fallo(estado, motivo=""):

        """Traduce el estado/mensaje de slskd a una explicación en español."""

        est = str(estado or "").lower()

        mot = str(motivo or "").strip()

        mot_l = mot.lower()

        if "rejected" in est:
            base = "rechazada por el usuario"

        elif "timedout" in est or "timed out" in est:
            base = "tiempo de espera agotado"

        elif "cancel" in est:
            base = "cancelada"

        elif "aborted" in est:
            base = "interrumpida"

        elif "error" in est or "failed" in est:
            base = "error de transferencia"

        else:
            base = str(estado or "fallo desconocido")

        razones = (
            ("only absolute paths", "slskd no pudo guardar el archivo: la ruta de descargas de slskd.yml es inválida (reinicia la app)"),
            ("file not shared", "el usuario ya no comparte ese archivo"),
            ("too many files", "el usuario alcanzó su límite de archivos"),
            ("too many megabytes", "el usuario alcanzó su límite de MB"),
            ("banned", "el usuario te bloqueó"),
            ("pending shutdown", "el usuario está cerrando su cliente"),
            ("connection refused", "no se pudo conectar con el usuario"),
            ("timed out", "el usuario no respondió"),
            ("connection", "falló la conexión con el usuario"),
        )

        detalle = ""

        for clave, texto in razones:

            if clave in mot_l:
                detalle = texto
                break

        if not detalle and mot:
            detalle = mot[:120]

        return f"{base}: {detalle}" if detalle else base

    @staticmethod
    def _transfer_exito(estado):

        estado = str(estado or "").lower()

        if SoulseekDownloader._transfer_fallo(estado):
            return False

        return (
            "succeeded" in estado
            or "success" in estado
            or estado.strip() == "completed"
        )

    # ========================================================
    # CARRERA DE CANDIDATOS
    # ========================================================

    def _quitar_transfer_previo(self, username, filename):

        # Si ya hubo un intento anterior con el mismo usuario y archivo, slskd
        # conserva el registro ("Completed, Rejected", "Completed, Cancelled"...).
        # Lo borramos para no confundirlo con el intento nuevo.

        try:

            transfer = self._buscar_transfer(
                username,
                filename,
                solo_usuario=True,
            )

            if not transfer:
                return

            info = self._extraer_progreso_transfer(transfer)

            if "completed" not in str(info.get("estado", "")).lower():
                return

            transfer_id = transfer.get("id")

            if not transfer_id and isinstance(transfer.get("transfer"), dict):
                transfer_id = transfer["transfer"].get("id")

            if not transfer_id:
                return

            self.session.delete(
                f"{self.base_url}/transfers/downloads/"
                f"{quote(str(username), safe='')}/{quote(str(transfer_id), safe='')}",
                params={"remove": "true"},
                timeout=5,
            )

        except Exception as e:
            print(f"[SLSKD] No se pudo limpiar un intento anterior: {e}")

    def _cancelar_candidato(self, candidato):

        try:

            transfer = candidato.get("transfer")

            if not transfer:
                transfer = self._buscar_transfer(
                    candidato["username"],
                    candidato["filename"],
                    solo_usuario=True,
                )

            if transfer:
                self.cancelar_transfer(transfer, candidato["username"])

        except Exception as e:
            print(f"[SLSKD] No se pudo cancelar un candidato: {e}")

    def _finalizar_descarga(self, candidato, emitir):

        filename = candidato["filename"]
        resultado = candidato["resultado"]
        nombre_local = candidato["nombre_local"]
        destino_final = candidato["destino_final"]

        # slskd marca la transferencia como terminada y, un instante después,
        # mueve el archivo a su carpeta final. Esperamos hasta 30 s a que aparezca.
        encontrado = self._esperar_archivo_descargado(nombre_local, filename)

        if encontrado:

            origen = Path(encontrado)

            archivo_final = origen

            try:

                destino_final.parent.mkdir(parents=True, exist_ok=True)

                try:
                    mismo = origen.resolve() == destino_final.resolve()
                except Exception:
                    mismo = False

                if not mismo:

                    if destino_final.exists():
                        # Ya había uno con ese nombre: se descarta el recién bajado.
                        origen.unlink()

                    else:
                        try:
                            shutil.move(str(origen), str(destino_final))
                        except Exception:
                            shutil.copy2(origen, destino_final)
                            origen.unlink()

                    archivo_final = destino_final

                    self._borrar_carpetas_vacias(origen.parent)

            except Exception as e:
                print(f"[SLSKD] No se pudo mover el archivo a su carpeta final: {e}")

            emitir("completado", estado="Completado", progreso=100)

            return {
                "estado": "completado",
                "archivo": str(archivo_final),
                "resultado": resultado,
            }

        print(
            "[SLSKD] La transferencia terminó en slskd, pero "
            f"no se encontró el archivo '{nombre_local}' en:"
        )

        for carpeta in self._carpetas_descarga():
            print(f"         {carpeta}")

        emitir("error", estado="Archivo no localizado")

        return {"estado": "error"}

    def _audio_existente(self, nombre_base):
        """Archivo de audio ya descargado con ese nombre (cualquier formato), o None."""

        try:
            for archivo in self.download_dir.glob(glob_literal(nombre_base) + ".*"):
                if archivo.is_file() and archivo.suffix.lower() in EXTENSIONES_AUDIO_FINALES:
                    return archivo
        except Exception:
            pass

        return None

    def _borrar_carpetas_vacias(self, carpeta):
        """Borra la carpeta de Soulseek si quedó vacía (y las de arriba, hasta la interna)."""

        try:
            raiz = self.staging_dir.resolve()
            actual = Path(carpeta).resolve()

            while actual != raiz and raiz in actual.parents:

                try:
                    actual.rmdir()  # solo funciona si está vacía
                except OSError:
                    break

                actual = actual.parent

        except Exception:
            pass

    def limpiar_staging(self):
        """
        Quita de la carpeta interna las subcarpetas de Soulseek que quedaron sueltas:
        borra los audios que ya están en la carpeta final y las carpetas vacías.
        """

        try:

            if not self.staging_dir.exists():
                return

            existentes = set()

            try:
                existentes = {
                    f.name.lower() for f in self.download_dir.iterdir() if f.is_file()
                }
            except Exception:
                pass

            for sub in self.staging_dir.iterdir():

                if not sub.is_dir() or sub.name.startswith("."):
                    continue

                for archivo in sorted(sub.rglob("*"), key=lambda x: len(x.parts), reverse=True):

                    try:

                        if archivo.is_file():

                            if archivo.name.lower() in existentes:
                                archivo.unlink()

                        elif archivo.is_dir():
                            archivo.rmdir()

                    except OSError:
                        pass

                try:
                    sub.rmdir()
                except OSError:
                    pass

        except Exception as e:
            print(f"[SLSKD] No se pudo limpiar la carpeta interna: {e}")

    def _descargar_candidatos(self, candidatos, control, emitir, misma_calidad=None, nombre_base=None):

        """
        Pide varios matches a la vez (CANDIDATOS_EN_CARRERA) y se queda con el
        primero que empieza a transferir. Descarta los que no avanzan y deja
        en consola (y en la tarjeta) el motivo de cada cancelación.
        """

        total = len(candidatos)

        pendientes = list(enumerate(candidatos, 1))

        activos = []

        ganador = None

        max_activos = max(1, int(CANDIDATOS_EN_CARRERA))

        if misma_calidad is None:
            misma_calidad = CARRERA_SOLO_MISMA_CALIDAD

        # Motivos de cada fallo/cancelación, para mostrarlos en la tarjeta.
        fallos = []
        aviso = ""

        def cancelar_activos(excepto=None, motivo=None):

            for otro in list(activos):

                if otro is excepto:
                    continue

                if motivo:

                    print(
                        f"[SLSKD] Candidato {otro['indice']}/{total} cancelado "
                        f"({motivo}): usuario {otro['username']}"
                    )

                self._cancelar_candidato(otro)

        while True:

            # CANCELACIÓN

            if control.cancelar.is_set():
                cancelar_activos(motivo="cancelaste la descarga")
                emitir("cancelado", estado="Cancelado")
                return {"estado": "cancelado"}

            # OTRO MATCH (botón de la tarjeta)

            if control.siguiente.is_set():

                control.siguiente.clear()

                print("[SLSKD] Se pidió otro match: se abandonan los candidatos actuales.")

                cancelar_activos(motivo="pediste otro match")
                activos.clear()
                ganador = None

                aviso = "Pediste otro match: se abandonaron los candidatos actuales"

                emitir(
                    "descargando",
                    estado="Probando otro match...",
                    progreso=0,
                    detalle=aviso,
                )

            # PEDIR NUEVOS CANDIDATOS (mientras no haya un ganador)

            if ganador is None:

                while len(activos) < max_activos and pendientes:

                    indice, resultado = pendientes[0]

                    if (
                        misma_calidad
                        and activos
                        and self._categoria_resultado(resultado)
                        != self._categoria_resultado(activos[0]["resultado"])
                    ):
                        break

                    pendientes.pop(0)

                    username = resultado.get("username", "")
                    filename = resultado.get("filename", "")

                    print(f"[SLSKD] Candidato {indice}/{total}:")
                    print(f"         {filename}")
                    print(f"         Usuario: {username}")
                    print(f"         Formato: {resultado.get('extension')}")
                    print(f"         Bitrate: {resultado.get('bitrate')}")
                    print(f"         Size: {resultado.get('size', 0)}")

                    if resultado.get("coincidencia"):
                        print(f"         Coincidencia: {resultado['coincidencia']}")

                    # YA EXISTE

                    nombre_local = normalizar_nombre_archivo(filename)

                    if nombre_base:
                        extension = Path(nombre_local).suffix
                        destino_final = self.download_dir / f"{nombre_base}{extension}"
                    else:
                        destino_final = self.download_dir / nombre_local

                    existente = self._audio_existente(nombre_base) if nombre_base else None

                    if existente is not None:
                        destino_final = existente

                    if destino_final.exists():

                        print(f"[SLSKD] Ya existe: {destino_final}")

                        cancelar_activos(motivo="la canción ya existía")

                        emitir("existente", estado="Ya descargada", progreso=100)

                        return {
                            "estado": "existente",
                            "archivo": str(destino_final),
                            "resultado": resultado,
                        }

                    # ENCOLAR

                    emitir(
                        "descargando",
                        estado=f"Solicitando ({indice}/{total})",
                        progreso=0,
                        intento=indice,
                        total=total,
                        detalle=aviso,
                    )

                    self._quitar_transfer_previo(username, filename)

                    datos_descarga = self.iniciar_descarga(resultado)

                    if datos_descarga is None:

                        print(
                            f"[SLSKD] Candidato {indice}/{total} descartado "
                            "(slskd no pudo encolar la descarga)."
                        )

                        aviso = f"Candidato {indice}/{total}: slskd no pudo encolar la descarga"

                        fallos.append(aviso)

                        continue

                    ahora = time.time()

                    activos.append(
                        {
                            "indice": indice,
                            "resultado": resultado,
                            "username": username,
                            "filename": filename,
                            "nombre_local": nombre_local,
                            "destino_final": destino_final,
                            "transfer": None,
                            "estado": "",
                            "progreso": 0.0,
                            "mejor_progreso": 0.0,
                            "inicio": ahora,
                            "ultimo_avance": ahora,
                            "ultimo_log": None,
                        }
                    )

            # NO QUEDAN CANDIDATOS

            if not activos:

                detalle = " | ".join(fallos[-3:]) or "ningún candidato pudo descargarse"

                print("[SLSKD] Todos los candidatos fallaron:")

                for linea in fallos:
                    print(f"         - {linea}")

                emitir(
                    "error",
                    estado="Todos los matches fallaron",
                    detalle=detalle,
                )

                return {"estado": "error"}

            # REVISAR CADA CANDIDATO ACTIVO

            ahora = time.time()

            for c in list(activos):

                if c not in activos:
                    continue

                transfer = self._buscar_transfer(
                    c["username"],
                    c["filename"],
                    solo_usuario=True,
                )

                if transfer:

                    info = self._extraer_progreso_transfer(transfer)

                    estado = str(info.get("estado", ""))

                    progreso = float(info.get("progreso", 0) or 0)

                    c["transfer"] = transfer
                    c["estado"] = estado
                    c["progreso"] = progreso

                    # El porcentaje avanzó: el candidato "está vivo".
                    if progreso > c["mejor_progreso"]:
                        c["mejor_progreso"] = progreso
                        c["ultimo_avance"] = ahora

                    marca = (estado, round(progreso, 1))

                    if marca != c["ultimo_log"]:

                        print(
                            "[SLSKD] "
                            f"{c['filename']} | "
                            f"{estado} | "
                            f"{progreso:.1f}%"
                        )

                        c["ultimo_log"] = marca

                    # FALLÓ

                    if self._transfer_fallo(estado):

                        explicacion = self._explicar_fallo(
                            estado,
                            info.get("motivo"),
                        )

                        print(
                            f"[SLSKD] Candidato {c['indice']}/{total} falló "
                            f"({explicacion}): usuario {c['username']}"
                        )

                        print(
                            f"[SLSKD]   estado de slskd: {estado} | "
                            f"mensaje: {info.get('motivo') or 'no informado'}"
                        )

                        aviso = f"Candidato {c['indice']}/{total}: {explicacion}"

                        fallos.append(aviso)

                        activos.remove(c)

                        if ganador is c:
                            ganador = None

                        continue

                    # TERMINÓ BIEN

                    if self._transfer_exito(estado):

                        cancelar_activos(
                            excepto=c,
                            motivo="otro candidato terminó primero",
                        )

                        return self._finalizar_descarga(c, emitir)

                    # EMPEZÓ A TRANSFERIR: es el ganador, se cancelan los demás

                    if ganador is None and progreso > 0:

                        ganador = c

                        if len(activos) > 1:

                            otros = len(activos) - 1

                            print(
                                f"[SLSKD] Candidato {c['indice']}/{total} "
                                "empezó a descargar primero."
                            )

                            cancelar_activos(
                                excepto=c,
                                motivo="otro candidato empezó a descargar primero",
                            )

                            activos[:] = [c]

                            aviso = (
                                f"Candidato {c['indice']}/{total} respondió primero; "
                                f"se cancelaron {otros} más"
                            )

                # DESCARTAR LOS QUE NO AVANZAN

                limite = (
                    SEGUNDOS_SIN_PROGRESO
                    if c["mejor_progreso"] > 0
                    else SEGUNDOS_SIN_ARRANQUE
                )

                # Si es el último candidato que queda, le damos más margen:
                # descartarlo significaría que la canción falla.
                if not pendientes and len(activos) == 1:
                    limite *= 3

                motivo = None

                if ahora - c["ultimo_avance"] > limite:

                    if c["mejor_progreso"] > 0:
                        motivo = (
                            f"la descarga se detuvo en {c['mejor_progreso']:.0f}% "
                            f"durante {int(limite)}s"
                        )

                    else:
                        motivo = (
                            f"no empezó a transferir en {int(limite)}s "
                            f"(estado: {c['estado'] or 'sin respuesta'})"
                        )

                elif ahora - c["inicio"] > TIMEOUT_DESCARGA_SEG:
                    motivo = "superó el tiempo máximo de descarga"

                if motivo:

                    print(
                        f"[SLSKD] Candidato {c['indice']}/{total} descartado "
                        f"({motivo}): usuario {c['username']}"
                    )

                    aviso = f"Candidato {c['indice']}/{total}: {motivo}"

                    fallos.append(aviso)

                    self._cancelar_candidato(c)

                    activos.remove(c)

                    if ganador is c:
                        ganador = None

            # MOSTRAR EN LA TARJETA EL CANDIDATO MÁS ADELANTADO

            if activos:

                if ganador is not None and ganador in activos:
                    rep = ganador

                else:
                    rep = max(
                        activos,
                        key=lambda x: (x["progreso"], -x["indice"]),
                    )

                emitir(
                    "descargando",
                    estado=rep["estado"] or f"Solicitando ({rep['indice']}/{total})",
                    progreso=rep["progreso"],
                    intento=rep["indice"],
                    total=total,
                    detalle=aviso,
                )

            time.sleep(1.0)

    # ========================================================
    # PROCESAR CANCIÓN
    # ========================================================

    def procesar_cancion(
        self,
        artista,
        titulo,
        actualizar_progreso=None,
        control=None,
        detalles=None,
    ):

        # detalles: diccionario opcional que se completa con la consulta y con
        # TODOS los archivos que devolvió la búsqueda (para revisarlos a mano).

        if control is None:
            control = ControlDescarga()

        def emitir(fase, **datos):

            if actualizar_progreso:

                payload = {"fase": fase, **datos}

                try:
                    actualizar_progreso(payload)

                except Exception as e:
                    print(f"[SLSKD] Error callback: {e}")

        # CANCELACIÓN

        if control.cancelar.is_set():
            emitir("cancelado", estado="Cancelado")
            return {"estado": "cancelado"}

        # BUSCAR

        emitir("buscando", estado="Buscando match...")

        # Se busca con el título SIN la versión ("Tema - Radio Edit" -> "Tema"):
        # así slskd devuelve más archivos, y luego el ranking prefiere los que
        # sí dicen la versión pedida.
        titulo_busqueda, _version = self._separar_version(titulo)

        search_id = self.iniciar_busqueda(artista, titulo_busqueda, control)

        if not search_id:
            emitir("error", estado="Error creando búsqueda")
            return {"estado": "error"}

        resultados = self.esperar_resultados(
            search_id,
            artista,
            titulo,
            control,
        )

        # CANCELACIÓN

        if control.cancelar.is_set():
            emitir("cancelado", estado="Cancelado")
            return {"estado": "cancelado"}

        # SIGUIENTE

        if control.siguiente.is_set():

            control.siguiente.clear()

            emitir("buscando", estado="Buscando otro match...")

            if len(resultados) > 1:
                resultados = resultados[1:]

            else:

                search_id_2 = self.iniciar_busqueda(artista, titulo_busqueda, control)

                if search_id_2:
                    search_id = search_id_2
                    resultados = self.esperar_resultados(
                        search_id_2,
                        artista,
                        titulo,
                        control,
                    )

        # Guardar lo que devolvió la búsqueda, para poder revisarlo a mano.
        if detalles is not None:

            detalles["crudos"] = list(self._crudos_busqueda.get(search_id, []))

            detalles["consulta"] = f"{artista} - {titulo_busqueda}"

        self._crudos_busqueda.pop(search_id, None)

        # SIN MATCH

        if not resultados:

            resumen = self._resumen_busqueda.get(search_id) or {}

            archivos = resumen.get("archivos", 0)
            respuestas = resumen.get("respuestas", 0)
            audio = resumen.get("audio", 0)

            if audio:
                detalle = (
                    f"{audio} archivos de audio de {respuestas} usuarios, "
                    f"ninguno coincide con '{artista} - {titulo_busqueda}'"
                )

            elif archivos:
                detalle = (
                    f"slskd encontró {archivos} archivos de {respuestas} "
                    "usuarios, pero no se pudieron leer a tiempo"
                )

            else:
                detalle = "nadie respondió a la búsqueda"

            print(f"[SLSKD] Sin match para {artista} - {titulo}: {detalle}")

            emitir(
                "sin_match",
                estado="No se encontró un match",
                detalle=detalle,
            )

            return {"estado": "sin_match"}

        # CANDIDATOS (se piden varios a la vez y gana el primero que avanza)

        resultado_final = self._descargar_candidatos(
            resultados[:MAX_CANDIDATOS],
            control,
            emitir,
            nombre_base=construir_nombre(artista, titulo),
        )

        if resultado_final is not None:
            return resultado_final

        emitir("error", estado="Todos los matches fallaron")

        return {"estado": "error"}

    # ========================================================
    # RESPALDO: YOUTUBE MUSIC (módulo youtube.py)
    # ========================================================

    def _youtube(self):

        if getattr(self, "_yt", None) is None:

            from youtube import YouTubeMusic

            self._yt = YouTubeMusic(self.download_dir, self.base_dir)

        return self._yt

    def youtube_disponible(self):
        """(True, 'ok') o (False, explicación de lo que falta instalar)."""

        try:
            return self._youtube().disponible()

        except ImportError:
            return False, "No se encontró el archivo youtube.py junto a gui.py."

        except Exception as e:
            return False, f"No se pudo preparar YouTube Music: {e}"

    def procesar_youtube(
        self,
        artista,
        titulo,
        album="",
        duracion_ms=0,
        actualizar_progreso=None,
        control=None,
    ):
        """Busca la canción en YouTube Music y la descarga como MP3 320 kbps."""

        try:

            return self._youtube().procesar_cancion(
                artista,
                titulo,
                album=album,
                duracion_ms=duracion_ms,
                actualizar_progreso=actualizar_progreso,
                control=control,
            )

        except ImportError:

            if actualizar_progreso:
                actualizar_progreso(
                    {
                        "fase": "error",
                        "estado": "YouTube Music no disponible",
                        "detalle": "No se encontró el archivo youtube.py junto a gui.py.",
                    }
                )

            return {"estado": "error"}

    # ========================================================
    # DESCARGA DE ARCHIVOS ELEGIDOS A MANO
    # ========================================================

    def procesar_resultados_manual(
        self,
        resultados,
        actualizar_progreso=None,
        control=None,
        artista=None,
        titulo=None,
    ):

        """
        Descarga archivos que el usuario eligió a mano de la lista de resultados.
        Si elige varios, se piden a la vez y se queda con el primero que avanza.
        """

        if control is None:
            control = ControlDescarga()

        def emitir(fase, **datos):

            if actualizar_progreso:

                try:
                    actualizar_progreso({"fase": fase, **datos})

                except Exception as e:
                    print(f"[SLSKD] Error callback: {e}")

        if control.cancelar.is_set():
            emitir("cancelado", estado="Cancelado")
            return {"estado": "cancelado"}

        candidatos = [r for r in resultados if not r.get("locked", False)]

        if not candidatos:

            emitir(
                "error",
                estado="Sin archivos descargables",
                detalle="Los archivos elegidos están bloqueados por su dueño",
            )

            return {"estado": "error"}

        # Elegidos a mano: compiten todos, sin importar la calidad.
        resultado = self._descargar_candidatos(
            candidatos[:MAX_CANDIDATOS],
            control,
            emitir,
            misma_calidad=False,
            nombre_base=construir_nombre(artista, titulo) if (artista or titulo) else None,
        )

        if resultado is not None:
            return resultado

        emitir("error", estado="Todos los matches fallaron")

        return {"estado": "error"}

    # ========================================================
    # PROCESAR VARIAS CANCIONES EN PARALELO
    # ========================================================

    def procesar_lote(
        self,
        canciones,
        max_simultaneas=MAX_SIMULTANEAS,
        actualizar_progreso=None,
        controles=None,
    ):
        """
        canciones: lista de tuplas (artista, titulo)

        actualizar_progreso: función que recibe (indice, payload),
        donde indice es la posición de la canción en la lista.

        controles: lista opcional de ControlDescarga, uno por canción
        (en el mismo orden). Sirve para cancelar o saltar al siguiente
        match de una canción concreta. Si no se pasa, se crean solos.

        Devuelve una lista con el resultado de cada canción, en el
        mismo orden que la lista de entrada.
        """

        total = len(canciones)

        if controles is None:
            controles = [ControlDescarga() for _ in range(total)]

        resultados = [None] * total

        def trabajo(indice, artista, titulo):

            def callback(payload):
                if actualizar_progreso:
                    actualizar_progreso(indice, payload)

            try:
                return self.procesar_cancion(
                    artista,
                    titulo,
                    actualizar_progreso=callback,
                    control=controles[indice],
                )

            except Exception as e:
                print(f"[SLSKD] Error procesando {artista} - {titulo}: {e}")
                return {"estado": "error"}

        with ThreadPoolExecutor(
            max_workers=max(1, int(max_simultaneas))
        ) as pool:

            futuros = {
                pool.submit(trabajo, i, artista, titulo): i
                for i, (artista, titulo) in enumerate(canciones)
            }

            for futuro in as_completed(futuros):
                resultados[futuros[futuro]] = futuro.result()

        return resultados

    # ========================================================
    # BUSCAR ARCHIVO DESCARGADO
    # ========================================================

    def _directorios_slskd(self):
        """Carpetas de descarga que usa slskd (se consulta a su API una sola vez)."""

        if self._dirs_slskd is not None:
            return self._dirs_slskd

        carpetas = []

        # 1. Lo que slskd dice tener configurado.
        try:
            respuesta = self.session.get(
                f"{self.base_url}/options",
                timeout=5,
            )

            if respuesta.ok:

                datos = respuesta.json()

                directorios = {}

                if isinstance(datos, dict):

                    for clave, valor in datos.items():

                        if str(clave).lower() == "directories" and isinstance(valor, dict):
                            directorios = valor

                for clave, valor in directorios.items():

                    if (
                        str(clave).lower() in ("downloads", "incomplete")
                        and isinstance(valor, str)
                        and valor.strip()
                    ):
                        carpetas.append(Path(valor.strip()))

        except Exception:
            pass

        # 2. Ubicaciones por defecto de slskd.
        localappdata = os.environ.get("LOCALAPPDATA")

        if localappdata:
            carpetas.append(Path(localappdata) / "slskd" / "downloads")
            carpetas.append(Path(localappdata) / "slskd" / "incomplete")

        carpetas.append(Path.home() / ".local" / "share" / "slskd" / "downloads")
        carpetas.append(self.slskd_dir / "downloads")

        # Sin repetidos y solo las que existen.
        vistas = set()
        resultado = []

        for carpeta in carpetas:

            try:
                clave = str(carpeta.resolve()).lower()

                if clave in vistas or not carpeta.exists():
                    continue

                vistas.add(clave)
                resultado.append(carpeta)

            except Exception:
                continue

        self._dirs_slskd = resultado

        return resultado

    def _carpetas_descarga(self):

        carpetas = [self.staging_dir, self.incomplete_dir, self.download_dir]

        carpetas.extend(self._directorios_slskd())

        carpetas.append(self.slskd_dir)

        vistas = set()
        resultado = []

        for carpeta in carpetas:

            try:
                clave = str(carpeta.resolve()).lower()

            except Exception:
                clave = str(carpeta).lower()

            if clave in vistas:
                continue

            vistas.add(clave)
            resultado.append(carpeta)

        return resultado

    def _buscar_archivo_descargado(
        self,
        nombre,
        filename_remoto=None,
        profundo=True,
    ):

        nombre_norm = str(nombre).lower()

        carpetas = self._carpetas_descarga()

        # Búsqueda rápida: slskd guarda en <descargas>/<última carpeta remota>/<archivo>
        if filename_remoto:

            partes = str(filename_remoto).replace("\\", "/").split("/")

            for carpeta in carpetas:

                candidatos = [carpeta / nombre]

                if len(partes) >= 2:
                    candidatos.insert(0, carpeta / partes[-2] / partes[-1])

                for candidato in candidatos:

                    try:

                        if candidato.is_file():
                            return str(candidato)

                    except OSError:
                        pass

        if not profundo:
            return None

        for carpeta in carpetas:

            if not carpeta.exists():
                continue

            try:

                for archivo in carpeta.rglob("*"):

                    if not archivo.is_file():
                        continue

                    if archivo.name.lower() == nombre_norm:
                        return str(archivo)

            except Exception:
                continue

        return None

    def _esperar_archivo_descargado(self, nombre, filename_remoto, max_seg=30):

        inicio = time.time()

        intento = 0

        while True:

            encontrado = self._buscar_archivo_descargado(
                nombre,
                filename_remoto,
                profundo=(intento % 3 == 0),
            )

            if encontrado:
                return encontrado

            if time.time() - inicio >= max_seg:
                return None

            time.sleep(1.0)

            intento += 1

    # ========================================================
    # LIMPIEZA DE INCOMPLETOS
    # ========================================================

    def limpiar_pendientes(self):

        if not self.incomplete_dir.exists():
            return

        for archivo in self.incomplete_dir.iterdir():

            try:

                if archivo.is_file():
                    archivo.unlink()

                elif archivo.is_dir():
                    shutil.rmtree(archivo)

            except Exception as e:
                print(f"[SLSKD] No se pudo eliminar {archivo}: {e}")

    # ========================================================
    # LIMPIEZA GENERAL
    # ========================================================

    def _transfers_activos(self, datos, usuario=None, acumulado=None):
        """Recorre la respuesta de /transfers/downloads y junta los que no terminaron."""

        if acumulado is None:
            acumulado = []

        if isinstance(datos, list):
            for x in datos:
                self._transfers_activos(x, usuario, acumulado)

        elif isinstance(datos, dict):

            usuario = datos.get("username") or usuario

            if datos.get("id") and datos.get("state") is not None:

                estado = str(datos.get("state") or "").lower()

                if "completed" not in estado:
                    acumulado.append((usuario, datos))

            for valor in datos.values():
                if isinstance(valor, (list, dict)):
                    self._transfers_activos(valor, usuario, acumulado)

        return acumulado

    def cerrar_todo(self, borrar_incompletas=True):
        """
        Cierre limpio al salir de la app: cancela lo que está descargando,
        cierra slskd y borra los archivos incompletos.
        """

        # 1) Cancelar en slskd lo que no terminó (con remove=true no quedan en la lista).
        try:
            for usuario, transfer in self._transfers_activos(self.obtener_descargas()):

                try:
                    self.session.delete(
                        f"{self.base_url}/transfers/downloads/"
                        f"{quote(str(usuario), safe='')}/{quote(str(transfer.get('id')), safe='')}",
                        params={"remove": "true"},
                        timeout=4,
                    )
                except Exception:
                    pass

        except Exception as e:
            print(f"[SLSKD] No se pudieron cancelar las descargas: {e}")

        # 2) Cerrar slskd (el que abrimos nosotros y cualquier otro que haya quedado).
        try:
            if self.slskd_process is not None and self.slskd_process.poll() is None:
                self.slskd_process.terminate()
        except Exception:
            pass

        self._cerrar_slskd_existente()

        # 3) Borrar lo incompleto (con slskd cerrado ya no está bloqueado).
        if borrar_incompletas:

            self.limpiar_pendientes()

            for carpeta in {self.download_dir, self.staging_dir}:
                for patron in ("*.part", "*.ytdl", "*.part-Frag*"):
                    for archivo in carpeta.glob(patron):
                        try:
                            archivo.unlink()
                        except Exception:
                            pass

        self.limpiar_staging()

    def limpiar_historial(self):

        self.limpiar_historial_slskd()

        self._busquedas_diagnosticadas.clear()


# ============================================================
# TEST DIRECTO
# ============================================================

if __name__ == "__main__":

    downloader = SoulseekDownloader()

    if downloader.iniciar_servidor_slskd():
        print("[SLSKD] Downloader listo.")

    else:
        print("[SLSKD] No se pudo iniciar slskd.")