"""
Respaldo para cuando una canción no está en Soulseek: se busca en YouTube Music
y se convierte a MP3 de 320 kbps con yt-dlp + ffmpeg.

Nota sobre la calidad: YouTube Music no tiene audio de 320 kbps. Su mejor audio
público es de unos 128-160 kbps (256 kbps con una cuenta Premium). El MP3 queda
codificado a 320 kbps, pero no puede sonar mejor que el original.

Requisitos (una sola vez):
    pip install yt-dlp
    ffmpeg y ffprobe instalados (por ejemplo:  winget install Gyan.FFmpeg ), o
    copiar ffmpeg.exe y ffprobe.exe en la misma carpeta que gui.py.
"""

import importlib
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote

try:
    import yt_dlp
except Exception:  # yt-dlp no está instalado
    yt_dlp = None

URL_YTDLP = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
URL_DENO = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

# Cada cuántos días se renueva yt-dlp (YouTube cambia seguido y las versiones viejas dan 403).
YT_DIAS_ACTUALIZAR = 3

_candado_componentes = threading.RLock()
_estado = {"actualizado_en_sesion": False, "ruta_cargada": None}


def _descargar_archivo(url, destino, timeout=60):
    """Descarga un archivo de forma atómica (primero a .tmp)."""

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(destino.suffix + ".tmp")

    pedido = urllib.request.Request(url, headers={"User-Agent": "Spotiseek"})

    with urllib.request.urlopen(pedido, timeout=timeout) as r, open(temporal, "wb") as f:
        shutil.copyfileobj(r, f)

    os.replace(temporal, destino)


def _cargar_ytdlp(ruta_zip):
    """Importa yt-dlp desde el archivo descargado (si es válido). Devuelve el módulo o None."""

    global yt_dlp

    ruta_zip = str(ruta_zip)

    if not os.path.isfile(ruta_zip):
        return None

    if _estado["ruta_cargada"] == ruta_zip and yt_dlp is not None:
        return yt_dlp

    anterior = yt_dlp

    try:
        for nombre in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.") or n.startswith("yt_dlp_ejs")]:
            del sys.modules[nombre]

        if ruta_zip in sys.path:
            sys.path.remove(ruta_zip)

        sys.path.insert(0, ruta_zip)
        importlib.invalidate_caches()

        modulo = importlib.import_module("yt_dlp")

        yt_dlp = modulo
        _estado["ruta_cargada"] = ruta_zip

        return modulo

    except Exception as e:
        print(f"[YT] No pude cargar el yt-dlp descargado: {e}")

        yt_dlp = anterior

        return None

from downloader import SoulseekDownloader, construir_nombre


# ============================================================
# CONFIGURACIÓN
# ============================================================

# Calidad del MP3 resultante (kbps).
YT_BITRATE_MP3 = "320"

# Cuántos resultados de la búsqueda se revisan.
YT_CANDIDATOS = 6

# Si Spotify informa la duración, se descartan los resultados que difieran más que esto.
YT_TOLERANCIA_DURACION_SEG = 15

# Cuántas descargas/conversiones a la vez (la conversión usa CPU y YouTube limita el ritmo).
YT_MAX_SIMULTANEAS = 2

# Para usar tu cuenta de YouTube (por ejemplo Premium, que da audio de 256 kbps),
# pon el navegador donde tienes la sesión iniciada: "chrome", "edge", "firefox", "brave"...
# None = sin cuenta.
YT_COOKIES_NAVEGADOR = None

# Palabras que indican otra versión (en vivo, cover, etc.). Se penalizan salvo que
# también estén en el título de Spotify.
PALABRAS_NO_DESEADAS = (
    "live", "cover", "karaoke", "sped", "slowed", "reverb",
    "nightcore", "8d", "instrumental", "remix", "mashup", "acoustic",
)


class _Cancelado(Exception):
    pass


def nombre_archivo(artista, titulo):
    """Nombre del archivo según el formato elegido en Configuración (sin caracteres prohibidos en Windows)."""

    return construir_nombre(artista, titulo)


# ============================================================
# YOUTUBE MUSIC
# ============================================================

class YouTubeMusic:

    def __init__(self, download_dir, base_dir):

        self.download_dir = Path(download_dir)
        self.base_dir = Path(base_dir)

        self._semaforo = threading.Semaphore(max(1, int(YT_MAX_SIMULTANEAS)))

    # ========================================================
    # COMPONENTES (yt-dlp actualizado + motor JavaScript)
    # ========================================================

    @property
    def carpeta_componentes(self):
        return self.base_dir / "componentes"

    def _ruta_ytdlp(self):
        return self.carpeta_componentes / "yt-dlp.zip"

    def _ruta_deno(self):
        return self.carpeta_componentes / ("deno.exe" if os.name == "nt" else "deno")

    def _runtimes_js(self):
        """Motores JavaScript disponibles (YouTube los exige para firmar las descargas)."""

        runtimes = {}

        deno = self._ruta_deno()

        if deno.is_file():
            runtimes["deno"] = {"path": str(deno)}

        elif shutil.which("deno"):
            runtimes["deno"] = {"path": shutil.which("deno")}

        for nombre in ("node", "bun", "quickjs"):

            ruta = shutil.which(nombre)

            if ruta:
                runtimes[nombre] = {"path": ruta}

        return runtimes

    def preparar_componentes(self, forzar=False):
        """Deja listo yt-dlp actualizado y un motor JS. Nunca lanza errores."""

        with _candado_componentes:

            try:

                zip_yt = self._ruta_ytdlp()

                vieja = (
                    not zip_yt.is_file()
                    or (time.time() - zip_yt.stat().st_mtime) > YT_DIAS_ACTUALIZAR * 86400
                )

                if forzar or vieja:

                    try:
                        print("[YT] Descargando la versión más reciente de yt-dlp...")
                        _descargar_archivo(URL_YTDLP, zip_yt)
                        _estado["ruta_cargada"] = None

                    except Exception as e:
                        print(f"[YT] No pude actualizar yt-dlp: {e}")

                if zip_yt.is_file():
                    _cargar_ytdlp(zip_yt)

                if os.name == "nt" and not self._runtimes_js():

                    try:
                        print("[YT] Descargando el motor JavaScript (deno)...")

                        temporal = self.carpeta_componentes / "deno.zip"

                        _descargar_archivo(URL_DENO, temporal, timeout=180)

                        with zipfile.ZipFile(temporal) as z:
                            with z.open("deno.exe") as origen, open(self._ruta_deno(), "wb") as f:
                                shutil.copyfileobj(origen, f)

                        os.remove(temporal)

                    except Exception as e:
                        print(f"[YT] No pude descargar deno: {e}")

            except Exception as e:
                print(f"[YT] Error preparando componentes: {e}")

    def _asegurar_componentes(self):
        """Prepara los componentes una vez por sesión (puede tardar: se usa desde hilos de trabajo)."""

        with _candado_componentes:

            if not _estado.get("preparado"):
                self.preparar_componentes()
                _estado["preparado"] = True

    def _actualizar_por_403(self):
        """Tras un error 403: baja yt-dlp de nuevo (una vez por sesión). True si hubo cambio."""

        if _estado["actualizado_en_sesion"]:
            return False

        _estado["actualizado_en_sesion"] = True

        self.preparar_componentes(forzar=True)

        return True

    # ========================================================
    # DISPONIBILIDAD
    # ========================================================

    def _buscar_programa(self, nombre):
        """Carpeta donde está ffmpeg/ffprobe (en el PATH o junto a la app)."""

        ruta = shutil.which(nombre)

        if ruta:
            return Path(ruta).parent

        carpetas = (
            self.base_dir,
            self.base_dir / "ffmpeg",
            self.base_dir / "ffmpeg" / "bin",
            self.base_dir / "bin",
        )

        for carpeta in carpetas:

            for extension in (".exe", ""):

                if (carpeta / f"{nombre}{extension}").is_file():
                    return carpeta

        return None

    def disponible(self):
        """Devuelve (True, 'ok') o (False, explicación de lo que falta)."""

        # Los componentes se preparan en segundo plano para no congelar la ventana.
        if not _estado.get("preparado") and not _estado.get("lanzado"):
            _estado["lanzado"] = True
            threading.Thread(target=self._asegurar_componentes, daemon=True).start()

        if yt_dlp is None:
            return False, (
                "Todavía no tengo yt-dlp: se descarga solo desde GitHub. "
                "Revisa tu conexión a internet y vuelve a intentarlo en un minuto."
            )

        faltan = [
            programa
            for programa in ("ffmpeg", "ffprobe")
            if self._buscar_programa(programa) is None
        ]

        if faltan:
            return False, (
                f"Falta {' y '.join(faltan)}. Instálalo con:  winget install Gyan.FFmpeg  "
                "(y vuelve a abrir la terminal), o descarga ffmpeg y copia ffmpeg.exe "
                "y ffprobe.exe en la misma carpeta que gui.py."
            )

        return True, "ok"

    def _opciones_base(self):

        opciones = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 3,
            "remote_components": ["ejs:github"],
        }

        runtimes = self._runtimes_js()

        if runtimes:
            opciones["js_runtimes"] = runtimes

        carpeta = self._buscar_programa("ffmpeg")

        if carpeta:
            opciones["ffmpeg_location"] = str(carpeta)

        if YT_COOKIES_NAVEGADOR:
            opciones["cookiesfrombrowser"] = (YT_COOKIES_NAVEGADOR,)

        return opciones

    # ========================================================
    # BÚSQUEDA
    # ========================================================

    @staticmethod
    def _url(entrada):

        id_video = entrada.get("id")

        if id_video:
            return f"https://www.youtube.com/watch?v={id_video}"

        return entrada.get("url") or entrada.get("webpage_url") or ""

    @staticmethod
    def _es_video(entrada):

        if not isinstance(entrada, dict):
            return False

        id_video = str(entrada.get("id") or "")

        # Los videos tienen un id de 11 caracteres; los álbumes, listas y canales no.
        return len(id_video) == 11 and entrada.get("_type") in (None, "url", "video")

    def _buscar_entradas(self, consulta):
        """Resultados de la búsqueda: primero YouTube Music (solo canciones), luego YouTube."""

        fuentes = (
            "https://music.youtube.com/search?q="
            + quote(consulta)
            + "&sp=EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D",
            f"ytsearch{YT_CANDIDATOS + 2}:{consulta}",
        )

        for fuente in fuentes:

            try:

                opciones = {
                    **self._opciones_base(),
                    "extract_flat": "in_playlist",
                    "playlistend": YT_CANDIDATOS + 2,
                    "skip_download": True,
                }

                with yt_dlp.YoutubeDL(opciones) as ydl:
                    info = ydl.extract_info(fuente, download=False)

                entradas = []

                def recolectar(items):

                    for item in items or []:

                        if not item:
                            continue

                        if item.get("entries"):
                            recolectar(item["entries"])

                        elif self._es_video(item):
                            entradas.append(item)

                recolectar((info or {}).get("entries"))

                if entradas:
                    return entradas

            except Exception as e:
                print(f"[YT] La búsqueda en {fuente[:28]}... falló: {e}")

        return []

    def _completar(self, entrada):
        """Si a un resultado le falta la duración o el canal, se consulta su ficha."""

        if entrada.get("duration") and (entrada.get("channel") or entrada.get("uploader")):
            return entrada

        try:

            opciones = {**self._opciones_base(), "skip_download": True}

            with yt_dlp.YoutubeDL(opciones) as ydl:
                info = ydl.extract_info(self._url(entrada), download=False)

            if info:

                extra = {
                    clave: info.get(clave)
                    for clave in ("duration", "channel", "uploader", "artist", "title")
                    if info.get(clave)
                }

                return {**entrada, **extra}

        except Exception as e:
            print(f"[YT] No se pudo leer la ficha de un resultado: {e}")

        return entrada

    # ========================================================
    # PUNTAJE DE CADA RESULTADO
    # ========================================================

    def _puntuar(self, entrada, artista, titulo, duracion_ms):
        """Devuelve un puntaje (mayor = mejor) o None si el resultado no sirve."""

        tokens = SoulseekDownloader._tokens

        base, version = SoulseekDownloader._separar_version(titulo)

        tokens_base = set(tokens(base))

        tokens_titulo_completo = set(tokens(titulo))

        tokens_artista_completo = set(tokens(artista))

        titulo_ent = str(entrada.get("title") or "")

        canal = str(
            entrada.get("artist")
            or entrada.get("channel")
            or entrada.get("uploader")
            or ""
        )

        tokens_ent = set(tokens(titulo_ent))
        tokens_canal = set(tokens(canal))

        if not tokens_base or not tokens_base <= tokens_ent:
            return None

        puntaje = 3.0

        # Artista: debe aparecer en el título o en el canal.
        opciones_artista = SoulseekDownloader._opciones_artista(artista)

        conjunto = tokens_ent | tokens_canal

        artista_ok = any(set(opcion) <= conjunto for opcion in opciones_artista)

        if artista_ok:
            puntaje += 3

        elif canal:
            # Se conoce el canal y no coincide con el artista: probablemente es otra canción.
            return None

        # Versiones que no se pidieron (en vivo, cover, remix...).
        for palabra in PALABRAS_NO_DESEADAS:

            if (
                palabra in tokens_ent
                and palabra not in tokens_titulo_completo
                and palabra not in tokens_artista_completo
            ):
                puntaje -= 4

        # La versión pedida (por ejemplo "extended mix") suma un poco.
        if version and set(version) <= tokens_ent:
            puntaje += 1

        # Duración
        duracion = entrada.get("duration")

        if duracion_ms and duracion:

            diferencia = abs(float(duracion) - duracion_ms / 1000.0)

            if diferencia <= 3:
                puntaje += 4

            elif diferencia <= 8:
                puntaje += 2

            elif diferencia > YT_TOLERANCIA_DURACION_SEG:
                return None

        # Canales "Topic" y audios oficiales suelen traer el audio original.
        if "topic" in tokens_canal or ("official" in tokens_ent and "audio" in tokens_ent):
            puntaje += 1

        return puntaje

    def buscar(self, artista, titulo, duracion_ms=0):
        """Mejores resultados de YouTube Music para la canción, de mejor a peor."""

        base, _version = SoulseekDownloader._separar_version(titulo)

        consulta = f"{artista} {base}"

        entradas = self._buscar_entradas(consulta)

        if not entradas:
            return []

        candidatas = []

        for entrada in entradas[:YT_CANDIDATOS]:

            # Si ya sabemos que el título no coincide, no se pierde tiempo en su ficha.
            tokens_ent = set(SoulseekDownloader._tokens(entrada.get("title") or ""))
            tokens_base = set(SoulseekDownloader._tokens(base))

            if tokens_ent and not tokens_base <= tokens_ent:
                continue

            entrada = self._completar(entrada)

            puntaje = self._puntuar(entrada, artista, titulo, duracion_ms)

            if puntaje is not None:
                candidatas.append((puntaje, entrada))

        candidatas.sort(key=lambda par: -par[0])

        return [entrada for _puntaje, entrada in candidatas[:3]]

    # ========================================================
    # DESCARGA
    # ========================================================

    def _limpiar_restos(self, nombre_base):
        """Borra archivos a medias (.part, .webm...) de una descarga fallida."""

        try:

            with os.scandir(self.download_dir) as it:

                for archivo in it:

                    if (
                        archivo.is_file()
                        and archivo.name.startswith(nombre_base + ".")
                        and not archivo.name.lower().endswith(".mp3")
                    ):
                        try:
                            os.remove(archivo.path)
                        except Exception:
                            pass

        except Exception:
            pass

    def _descargar(self, entrada, artista, titulo, album, emitir, control, clientes=None):
        """Descarga y convierte un resultado. Devuelve la ruta del MP3."""

        nombre_base = nombre_archivo(artista, titulo)

        destino = self.download_dir / f"{nombre_base}.mp3"

        # En la plantilla de yt-dlp el signo % es especial.
        plantilla = str(self.download_dir / f"{nombre_base.replace('%', '%%')}.%(ext)s")

        ultimo = {"t": 0.0, "pct": -10.0}

        def avisar(porcentaje, detalle):

            ahora = time.time()

            if ahora - ultimo["t"] < 0.5 and abs(porcentaje - ultimo["pct"]) < 2:
                return

            ultimo["t"] = ahora
            ultimo["pct"] = porcentaje

            emitir(
                "descargando",
                estado="InProgress",
                progreso=porcentaje,
                intento=1,
                total=1,
                detalle=detalle,
            )

        def hook(d):

            if control is not None and control.cancelar.is_set():
                raise _Cancelado()

            estado = d.get("status")

            if estado == "downloading":

                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

                hecho = d.get("downloaded_bytes") or 0

                porcentaje = (hecho / total * 90.0) if total else 0.0

                avisar(porcentaje, "YouTube Music: descargando el audio...")

            elif estado == "finished":
                avisar(92.0, "YouTube Music: convirtiendo a MP3 320...")

        def hook_conversion(d):

            if d.get("status") == "started":
                avisar(95.0, f"YouTube Music: convirtiendo a MP3 {YT_BITRATE_MP3}...")

        opciones = {
            **self._opciones_base(),
            "format": "bestaudio/best",
            "outtmpl": plantilla,
            "progress_hooks": [hook],
            "postprocessor_hooks": [hook_conversion],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(YT_BITRATE_MP3),
                }
            ],
            # Etiquetas del MP3 con los datos de Spotify.
            "postprocessor_args": {
                "ffmpeg": [
                    "-metadata", f"title={titulo}",
                    "-metadata", f"artist={artista}",
                    "-metadata", f"album={album or ''}",
                ]
            },
        }

        if clientes:
            opciones["extractor_args"] = {"youtube": {"player_client": list(clientes)}}

        try:

            with yt_dlp.YoutubeDL(opciones) as ydl:
                ydl.download([self._url(entrada)])

        except Exception:

            self._limpiar_restos(nombre_base)

            if control is not None and control.cancelar.is_set():
                raise _Cancelado()

            raise

        if destino.exists():
            return destino

        # Por si la extensión quedó distinta.
        with os.scandir(self.download_dir) as it:

            for archivo in it:

                if archivo.name.startswith(nombre_base + ".") and archivo.name.lower().endswith(".mp3"):
                    return Path(archivo.path)

        self._limpiar_restos(nombre_base)

        raise RuntimeError("no se generó el archivo MP3")

    # ========================================================
    # PROCESO COMPLETO DE UNA CANCIÓN
    # ========================================================

    def procesar_cancion(
        self,
        artista,
        titulo,
        album="",
        duracion_ms=0,
        actualizar_progreso=None,
        control=None,
    ):

        def emitir(fase, **datos):

            if actualizar_progreso:

                try:
                    actualizar_progreso({"fase": fase, **datos})

                except Exception as e:
                    print(f"[YT] Error callback: {e}")

        def cancelado():
            return control is not None and control.cancelar.is_set()

        if cancelado():
            emitir("cancelado", estado="Cancelado")
            return {"estado": "cancelado"}

        self._asegurar_componentes()

        ok, mensaje = self.disponible()

        if not ok:

            print(f"[YT] {mensaje}")

            emitir("error", estado="YouTube Music no disponible", detalle=mensaje)

            return {"estado": "error", "detalle": mensaje}

        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Ya descargada
        nombre_base = nombre_archivo(artista, titulo)

        destino = self.download_dir / f"{nombre_base}.mp3"

        if destino.exists():

            print(f"[YT] Ya existe: {destino}")

            emitir("existente", estado="Ya descargada", progreso=100)

            return {
                "estado": "existente",
                "archivo": str(destino),
                "resultado": {"fuente": "YouTube Music", "extension": ".mp3"},
            }

        # Buscar
        emitir(
            "buscando",
            estado="Buscando en YouTube Music...",
            detalle="YouTube Music: buscando la canción...",
        )

        print(f"[YT] Buscando: {artista} - {titulo}")

        candidatos = self.buscar(artista, titulo, duracion_ms)

        if cancelado():
            emitir("cancelado", estado="Cancelado")
            return {"estado": "cancelado"}

        if not candidatos:

            detalle = "No encontré en YouTube Music un resultado que coincida con el artista y el título."

            print(f"[YT] {detalle}")

            emitir("sin_match", estado="No se encontró en YouTube Music", detalle=detalle)

            return {"estado": "sin_match", "detalle": detalle}

        # Descargar (se prueba cada candidato hasta que uno funcione)
        ultimo_error = ""

        for numero, entrada in enumerate(candidatos, 1):

            titulo_video = str(entrada.get("title") or "")

            print(f"[YT] Candidato {numero}/{len(candidatos)}: {titulo_video} ({self._url(entrada)})")

            try:

                with self._semaforo:

                    if cancelado():
                        raise _Cancelado()

                    try:
                        ruta = self._descargar(entrada, artista, titulo, album, emitir, control)

                    except _Cancelado:
                        raise

                    except Exception as e403:

                        if "403" in str(e403) and self._actualizar_por_403():

                            emitir(
                                "descargando",
                                estado="InProgress",
                                progreso=0,
                                intento=1,
                                total=1,
                                detalle="YouTube: actualizando componentes y reintentando...",
                            )

                            ruta = self._descargar(entrada, artista, titulo, album, emitir, control)

                        elif "403" in str(e403):

                            # Último recurso: otros clientes de YouTube
                            ruta = self._descargar(
                                entrada, artista, titulo, album, emitir, control,
                                clientes=["tv", "web_safari", "mweb"],
                            )

                        else:
                            raise

                emitir("completado", estado="Completado", progreso=100)

                return {
                    "estado": "completado",
                    "archivo": str(ruta),
                    "resultado": {
                        "fuente": "YouTube Music",
                        "extension": ".mp3",
                        "bitrate": float(YT_BITRATE_MP3),
                        "titulo_origen": titulo_video,
                    },
                }

            except _Cancelado:

                emitir("cancelado", estado="Cancelado")

                return {"estado": "cancelado"}

            except Exception as e:

                ultimo_error = str(e)

                print(f"[YT] Candidato {numero} falló: {e}")

        detalle = (
            "YouTube falló al descargar. La app ya intentó actualizar sus componentes; "
            "si sigue pasando, vuelve a intentarlo más tarde. "
            f"Último error: {ultimo_error[:120]}"
        )

        emitir("error", estado="Falló la descarga", detalle=detalle)

        return {"estado": "error", "detalle": detalle}