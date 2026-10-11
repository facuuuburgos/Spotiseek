import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import ttk

import customtkinter as ctk

from downloader import (
    SoulseekDownloader, ControlDescarga, MAX_SIMULTANEAS, MAX_EN_PROCESO,
    AJUSTES_PREDETERMINADOS, BITRATES_YOUTUBE, construir_nombre,
)
import actualizador


# ============================================================
# CONFIGURACIÓN VISUAL
# ============================================================

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Sin esto, Windows agrupa la ventana bajo "Python" y muestra su icono en la barra de tareas.
if sys.platform.startswith("win"):
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Spotiseek.App")
    except Exception:
        pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ============================================================
# SPOTIFY: canciones, álbumes, EPs y playlists
# ============================================================

PATRON_SPOTIFY = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[A-Za-z\-]+/)?(?:embed/)?|spotify:)"
    r"(playlist|track|album)[/:]([A-Za-z0-9]{10,})"
)

NOMBRES_TIPO = {"track": "canción", "album": "álbum", "playlist": "playlist"}


def analizar_enlace_spotify(texto):
    """Devuelve (tipo, id) para un enlace de canción, álbum/EP o playlist; None si no lo es."""

    m = PATRON_SPOTIFY.search(str(texto or ""))

    if not m:
        return None

    return m.group(1), m.group(2)


def _buscar_clave(obj, clave, profundidad=0):
    """Busca la primera clave con ese nombre dentro de un JSON anidado."""

    if profundidad > 12:
        return None

    if isinstance(obj, dict):

        if clave in obj:
            return obj[clave]

        for valor in obj.values():
            r = _buscar_clave(valor, clave, profundidad + 1)
            if r is not None:
                return r

    elif isinstance(obj, list):

        for valor in obj:
            r = _buscar_clave(valor, clave, profundidad + 1)
            if r is not None:
                return r

    return None


def _nombres(valor):
    """Artistas: acepta texto, lista de textos o lista de {name: ...}."""

    if isinstance(valor, str):
        return valor

    if isinstance(valor, list):
        nombres = []

        for v in valor:
            if isinstance(v, dict):
                v = v.get("name") or v.get("title")
            if v:
                nombres.append(str(v))

        return ", ".join(nombres)

    return ""


def _limpiar(texto):
    return str(texto or "").replace("\xa0", " ").strip()


def _numero_ms(valor):
    try:
        return int(float(valor or 0))
    except (TypeError, ValueError):
        return 0


def _pista(nombre, artista, album, duracion):
    return {
        "nombre": _limpiar(nombre),
        "artista": _limpiar(artista),
        "album": _limpiar(album) or "Álbum",
        "duracion_ms": _numero_ms(duracion),
    }


def _pistas_de_json(datos, tipo):
    """Interpreta los distintos formatos de datos que usa la página incrustada de Spotify."""

    entidad = _buscar_clave(datos, "entity")
    entidad = entidad if isinstance(entidad, dict) else {}

    titulo_coleccion = _limpiar(entidad.get("name") or entidad.get("title"))

    pistas = []

    lista = _buscar_clave(datos, "trackList")

    # Formato actual: álbum y playlist traen "trackList"
    if isinstance(lista, list) and lista:

        for t in lista:

            if not isinstance(t, dict):
                continue

            album = ""

            if tipo == "album":
                album = titulo_coleccion
            else:
                album = (t.get("entity", {}) or {}).get("release", {}).get("title", "") if isinstance(t.get("entity"), dict) else ""

            pistas.append(
                _pista(
                    t.get("title") or t.get("name"),
                    t.get("subtitle") or _nombres(t.get("artists")),
                    album,
                    t.get("duration") or t.get("duration_ms"),
                )
            )

        return titulo_coleccion, pistas

    # Formato antiguo: tracks.items[]
    items = None
    pistas_obj = _buscar_clave(datos, "tracks")

    if isinstance(pistas_obj, dict):
        items = pistas_obj.get("items")
    elif isinstance(pistas_obj, list):
        items = pistas_obj

    if isinstance(items, list) and items:

        for it in items:

            t = it.get("track", it) if isinstance(it, dict) else None

            if not isinstance(t, dict):
                continue

            album = (t.get("album") or {}).get("name", "") if isinstance(t.get("album"), dict) else titulo_coleccion

            pistas.append(
                _pista(t.get("name"), _nombres(t.get("artists")), album, t.get("duration_ms") or t.get("duration"))
            )

        return titulo_coleccion or _limpiar(datos.get("name") if isinstance(datos, dict) else ""), pistas

    # Una sola canción
    nombre = entidad.get("name") or entidad.get("title")
    artista = _nombres(entidad.get("artists")) or entidad.get("subtitle") or ""

    if not nombre and isinstance(datos, dict) and datos.get("type") == "track":
        nombre = datos.get("name")
        artista = _nombres(datos.get("artists"))
        entidad = datos

    if nombre:
        album = ""
        if isinstance(entidad.get("album"), dict):
            album = entidad["album"].get("name", "")

        pistas.append(_pista(nombre, artista, album, entidad.get("duration") or entidad.get("duration_ms")))

    return titulo_coleccion, pistas


def _datos_de_html(html):
    """Saca el JSON que Spotify incluye en la página incrustada."""

    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)

    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    m = re.search(r'<script id="resource" type="application/json">(.*?)</script>', html, re.S)

    if m:
        crudo = m.group(1)

        for texto in (crudo, urllib.parse.unquote(crudo)):
            try:
                return json.loads(texto)
            except Exception:
                continue

    return None


def _meta(html, propiedad):
    m = re.search(
        r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(propiedad), html
    )

    if not m:
        m = re.search(
            r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % re.escape(propiedad), html
        )

    if not m:
        return ""

    import html as modulo_html

    return modulo_html.unescape(m.group(1))


def _descargar_texto(url):
    pedido = urllib.request.Request(url, headers=HEADERS)

    with urllib.request.urlopen(pedido, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def leer_spotify(tipo, identificador):
    """
    Devuelve (nombre_de_la_coleccion, [canciones]) para una canción, álbum/EP o playlist.
    Lanza una excepción con un mensaje claro si no se pudo leer.
    """

    pistas = []
    titulo = ""

    html = _descargar_texto(f"https://open.spotify.com/embed/{tipo}/{identificador}")

    datos = _datos_de_html(html)

    if datos is not None:
        titulo, pistas = _pistas_de_json(datos, tipo)

    # Respaldo para una sola canción: datos de la página normal (etiquetas og:)
    if not pistas and tipo == "track":

        try:
            pagina = _descargar_texto(f"https://open.spotify.com/track/{identificador}")

            nombre = _meta(pagina, "og:title")
            descripcion = _meta(pagina, "og:description")

            artista = ""

            if descripcion:
                artista = descripcion.split("·")[0].strip()

            if nombre and artista:
                pistas = [_pista(nombre, artista, "", 0)]

        except Exception:
            pass

    pistas = [p for p in pistas if p["nombre"] and p["artista"]]

    if not pistas:
        raise ValueError(
            "No se encontraron canciones en ese enlace. Revisa que sea público "
            "(los álbumes, playlists y canciones de Spotify se pueden compartir con «Copiar enlace»)."
        )

    for i, p in enumerate(pistas, 1):
        p["id"] = i
        p["busqueda"] = f"{p['artista']} - {p['nombre']}"
        p["estado"] = "Pendiente"

    return titulo, pistas



# ============================================================
# ICONOS DIBUJADOS (nítidos en cualquier sistema, sin emojis)
# ============================================================

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk

    HAY_PIL = True
except Exception:
    HAY_PIL = False

_SUPER = 8
_cache_pil = {}


def _rgb(color):
    color = str(color)

    if color == "white":
        return (255, 255, 255)

    color = color.lstrip("#")

    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def pil_icono(nombre, tam, color):
    """Dibuja un icono sencillo con suavizado (PIL). Devuelve None si no hay PIL."""

    if not HAY_PIL:
        return None

    clave = ("i", nombre, tam, color)

    if clave in _cache_pil:
        return _cache_pil[clave]

    S = tam * _SUPER
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = _rgb(color) + (255,)
    w = max(2, int(S * 0.11))

    def P(x, y):
        return (x * S, y * S)

    if nombre == "cruz":
        d.line([P(0.26, 0.26), P(0.74, 0.74)], fill=col, width=w)
        d.line([P(0.74, 0.26), P(0.26, 0.74)], fill=col, width=w)

    elif nombre == "play":
        d.polygon([P(0.30, 0.18), P(0.30, 0.82), P(0.84, 0.50)], fill=col)

    elif nombre == "siguiente":
        d.polygon([P(0.16, 0.20), P(0.16, 0.80), P(0.62, 0.50)], fill=col)
        d.rectangle([P(0.68, 0.20), P(0.82, 0.80)], fill=col)

    elif nombre == "lista":
        for y, x2 in ((0.26, 0.82), (0.50, 0.82), (0.74, 0.60)):
            d.line([P(0.18, y), P(x2, y)], fill=col, width=w)

    elif nombre == "reintentar":
        d.arc([P(0.16, 0.16), P(0.84, 0.84)], start=40, end=330, fill=col, width=w)
        # punta de flecha al final del arco
        import math

        a = math.radians(330)
        cx, cy, r = 0.5, 0.5, 0.34
        px, py = cx + r * math.cos(a), cy + r * math.sin(a)
        tx, ty = -math.sin(a), math.cos(a)
        nx, ny = math.cos(a), math.sin(a)
        d.polygon(
            [
                P(px + tx * 0.20, py + ty * 0.20),
                P(px + nx * 0.16, py + ny * 0.16),
                P(px - nx * 0.16, py - ny * 0.16),
            ],
            fill=col,
        )

    elif nombre == "usuario":
        d.ellipse([P(0.33, 0.14), P(0.67, 0.48)], fill=col)
        d.pieslice([P(0.16, 0.54), P(0.84, 1.20)], start=180, end=360, fill=col)

    elif nombre == "carpeta":
        d.rounded_rectangle([P(0.12, 0.20), P(0.46, 0.42)], radius=int(S * 0.05), fill=col)
        d.rounded_rectangle([P(0.12, 0.30), P(0.88, 0.80)], radius=int(S * 0.07), fill=col)

    elif nombre == "buscar":
        d.ellipse([P(0.16, 0.16), P(0.62, 0.62)], outline=col, width=w)
        d.line([P(0.56, 0.56), P(0.84, 0.84)], fill=col, width=w)

    elif nombre == "descargar":
        d.line([P(0.50, 0.14), P(0.50, 0.58)], fill=col, width=w)
        d.polygon([P(0.26, 0.46), P(0.74, 0.46), P(0.50, 0.74)], fill=col)
        d.line([P(0.18, 0.86), P(0.82, 0.86)], fill=col, width=w)

    elif nombre == "papelera":
        d.line([P(0.18, 0.28), P(0.82, 0.28)], fill=col, width=w)
        d.line([P(0.38, 0.16), P(0.62, 0.16)], fill=col, width=w)
        d.line([P(0.28, 0.28), P(0.32, 0.84), P(0.68, 0.84), P(0.72, 0.28)], fill=col, width=w, joint="curve")

    elif nombre == "check":
        d.line([P(0.18, 0.54), P(0.42, 0.76), P(0.82, 0.28)], fill=col, width=w, joint="curve")

    elif nombre == "pausa":
        d.rounded_rectangle([P(0.24, 0.18), P(0.43, 0.82)], radius=int(S * 0.04), fill=col)
        d.rounded_rectangle([P(0.57, 0.18), P(0.76, 0.82)], radius=int(S * 0.04), fill=col)

    elif nombre == "engranaje":
        import math

        for k in range(8):
            a = math.radians(k * 45)
            d.line(
                [P(0.5 + 0.30 * math.cos(a), 0.5 + 0.30 * math.sin(a)),
                 P(0.5 + 0.43 * math.cos(a), 0.5 + 0.43 * math.sin(a))],
                fill=col, width=int(w * 1.7),
            )
        d.ellipse([P(0.22, 0.22), P(0.78, 0.78)], outline=col, width=w)
        d.ellipse([P(0.42, 0.42), P(0.58, 0.58)], fill=col)

    elif nombre == "casilla":
        d.rounded_rectangle([P(0.14, 0.14), P(0.86, 0.86)], radius=int(S * 0.12), outline=col, width=w)
        d.line([P(0.31, 0.52), P(0.45, 0.67), P(0.70, 0.35)], fill=col, width=w, joint="curve")

    img = img.resize((tam, tam), Image.LANCZOS)
    _cache_pil[clave] = img

    return img


def pil_fondo(ancho, alto, radio, color, borde=None):
    """Rectángulo con esquinas redondeadas y bordes suaves."""

    if not HAY_PIL:
        return None

    ancho, alto, radio = int(ancho), int(alto), int(radio)

    clave = ("f", ancho, alto, radio, color, borde)

    if clave in _cache_pil:
        return _cache_pil[clave]

    k = 4
    img = Image.new("RGBA", (ancho * k, alto * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if borde:
        d.rounded_rectangle(
            [0, 0, ancho * k - 1, alto * k - 1], radius=radio * k, fill=_rgb(borde) + (255,)
        )
        d.rounded_rectangle(
            [k, k, ancho * k - 1 - k, alto * k - 1 - k],
            radius=max(0, (radio - 1) * k),
            fill=_rgb(color) + (255,),
        )
    else:
        d.rounded_rectangle(
            [0, 0, ancho * k - 1, alto * k - 1], radius=radio * k, fill=_rgb(color) + (255,)
        )

    img = img.resize((ancho, alto), Image.LANCZOS)
    _cache_pil[clave] = img

    return img


def icono_ctk(nombre, px, color):
    """Icono para botones de customtkinter (None si no hay PIL)."""

    img = pil_icono(nombre, px * 3, color)

    if img is None:
        return None

    return ctk.CTkImage(light_image=img, dark_image=img, size=(px, px))


TEXTO_BOTON_DESCARGAR = "Descargar lista (MP3 320 kbps)"


# ============================================================
# PLANIFICADOR: reparte las canciones entre varios hilos
# ============================================================

# ============================================================
# TEMA VISUAL (Spotiseek)
# Para cambiar la apariencia de toda la app, edita estos colores.
# ============================================================

C_FONDO = "#0e1016"            # fondo de la ventana y de la lista
C_FONDO_PROFUNDO = "#090b10"   # zonas hundidas
C_PANEL = "#161a23"            # paneles (cabecera, barra inferior)
C_FILA = "#141821"             # fila de la lista
C_FILA_HOVER = "#1c2230"       # fila bajo el mouse
C_SELECCION = "#2a2563"        # fila seleccionada
C_SELECCION_HOVER = "#312b76"
C_BORDE = "#252b3a"            # bordes finos
C_BOTON = "#222838"            # botones secundarios
C_BOTON_HOVER = "#2f3748"
C_TEXTO = "#eef0f7"            # texto principal
C_TEXTO_2 = "#a9b0c4"          # texto secundario
C_TEXTO_3 = "#7d869c"          # texto atenuado
C_TEXTO_4 = "#5b6379"
C_ACENTO = "#7c5cff"           # violeta de la marca
C_ACENTO_HOVER = "#6a49f0"
C_TEAL = "#14c8b0"             # turquesa de la marca
C_DESCARGANDO = "#a695ff"
C_OK = "#34d399"
C_AVISO = "#fbbf24"
C_ERROR = "#f4616d"
C_ERROR_HOVER = "#d9485a"
C_CTA = "#0f9d8a"              # botón principal de descarga
C_CTA_HOVER = "#0c7f70"
C_YT = "#e5484d"
C_YT_HOVER = "#c93a3f"

NOMBRE_APP = "Spotiseek"



# ============================================================
# RECURSOS (logo, icono) Y VENTANAS
# ============================================================

def _carpetas_recursos():
    carpetas = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        carpetas.append(Path(meipass))

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve().parent
        carpetas += [exe, exe / "_internal"]

    aqui = Path(__file__).resolve().parent
    carpetas += [aqui, aqui.parent]

    return carpetas


def ruta_recurso(relativa):
    for carpeta in _carpetas_recursos():
        ruta = carpeta / relativa
        if ruta.exists():
            return ruta
    return None


def cargar_logo(px):
    """Logo como imagen de customtkinter (None si no se puede cargar)."""
    try:
        from PIL import Image

        ruta = ruta_recurso("assets/logo_simbolo.png") or ruta_recurso("assets/logo.png")
        if ruta is None:
            return None

        imagen = Image.open(ruta)
        return ctk.CTkImage(light_image=imagen, dark_image=imagen, size=(px, px))

    except Exception as e:
        print(f"[GUI] No se pudo cargar el logo: {e}")
        return None


def poner_icono(ventana):
    """Icono de la ventana (se repite a los 300 ms porque customtkinter pone el suyo a los 200)."""

    def aplicar():
        try:
            if sys.platform.startswith("win"):
                ruta = ruta_recurso("assets/icono.ico")
                if ruta:
                    ventana.iconbitmap(str(ruta))
                    return

            ruta = ruta_recurso("assets/logo_128.png")
            if ruta:
                ventana._icono_foto = tk.PhotoImage(file=str(ruta))
                ventana.iconphoto(True, ventana._icono_foto)

        except Exception:
            pass

    aplicar()

    try:
        ventana.after(300, aplicar)
    except Exception:
        pass


def centrar_ventana(ventana, ancho, alto, referencia=None):
    """Coloca la ventana centrada en la pantalla o sobre otra ventana."""

    try:
        ventana.update_idletasks()
    except Exception:
        pass

    try:
        esc = float(ctk.ScalingTracker.get_window_scaling(ventana))
    except Exception:
        esc = 1.0

    ancho_px = int(ancho * esc)
    alto_px = int(alto * esc)

    try:
        if referencia is not None and referencia.winfo_viewable():
            x = referencia.winfo_rootx() + (referencia.winfo_width() - ancho_px) // 2
            y = referencia.winfo_rooty() + (referencia.winfo_height() - alto_px) // 2
        else:
            x = (ventana.winfo_screenwidth() - ancho_px) // 2
            y = (ventana.winfo_screenheight() - alto_px) // 2

        x = max(0, x)
        y = max(0, y)

        ventana.geometry(f"{ancho}x{alto}+{x}+{y}")

    except Exception:
        ventana.geometry(f"{ancho}x{alto}")


# ============================================================
# VENTANAS DE MENSAJE CON EL ESTILO DE LA APP
# (reemplazan a tkinter.messagebox, con las mismas funciones)
# ============================================================

def _insignia_mensaje(tipo, px, color):
    """Círculo de color con símbolo (i, !, x, ?) dibujado sin depender de fuentes del sistema."""

    k = 4
    t = px * k
    img = Image.new("RGBA", (t, t), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.ellipse((k, k, t - k, t - k), fill=color)

    blanco = (255, 255, 255, 255)
    c = t / 2

    if tipo == "info":
        d.ellipse((c - t * 0.06, t * 0.22, c + t * 0.06, t * 0.34), fill=blanco)
        d.rounded_rectangle((c - t * 0.055, t * 0.42, c + t * 0.055, t * 0.76), radius=t * 0.03, fill=blanco)

    elif tipo == "aviso":
        d.rounded_rectangle((c - t * 0.06, t * 0.2, c + t * 0.06, t * 0.58), radius=t * 0.03, fill=blanco)
        d.ellipse((c - t * 0.07, t * 0.66, c + t * 0.07, t * 0.80), fill=blanco)

    elif tipo == "error":
        a, b = t * 0.31, t * 0.69
        d.line((a, a, b, b), fill=blanco, width=int(t * 0.1))
        d.line((a, b, b, a), fill=blanco, width=int(t * 0.1))

    else:  # pregunta
        fuente = None
        for nombre in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                fuente = ImageFont.truetype(nombre, int(t * 0.62))
                break
            except Exception:
                continue
        if fuente is not None:
            d.text((c, c), "?", font=fuente, fill=blanco, anchor="mm")
        else:
            d.ellipse((c - t * 0.07, t * 0.66, c + t * 0.07, t * 0.80), fill=blanco)
            d.rounded_rectangle((c - t * 0.06, t * 0.2, c + t * 0.06, t * 0.58), radius=t * 0.03, fill=blanco)

    img = img.resize((px, px), Image.LANCZOS)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(px, px))


class VentanaMensaje(ctk.CTkToplevel):
    """Mensaje modal con el diseño de la app. Guarda la respuesta en self.resultado."""

    def __init__(self, parent, titulo, mensaje, tipo="info", botones=("Aceptar",), principal=0, cancelar=None):

        try:
            if parent is None:
                parent = tk._default_root
        except Exception:
            parent = None

        super().__init__(parent)

        self.resultado = cancelar
        self._cancelar = cancelar

        self.withdraw()
        self.title(titulo)
        poner_icono(self)
        self.resizable(False, False)
        self.configure(fg_color=C_FONDO)

        colores = {"info": C_ACENTO, "aviso": C_AVISO, "error": C_ERROR, "pregunta": C_ACENTO}
        color = colores.get(tipo, C_ACENTO)

        marco = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=10, border_width=1, border_color=C_BORDE)
        marco.pack(fill="both", expand=True, padx=12, pady=12)

        cuerpo = ctk.CTkFrame(marco, fg_color="transparent")
        cuerpo.pack(fill="both", expand=True, padx=18, pady=(18, 8))

        ctk.CTkLabel(
            cuerpo, image=_insignia_mensaje(tipo, 40, color), text="",
        ).pack(side="left", anchor="n", padx=(0, 14))

        textos = ctk.CTkFrame(cuerpo, fg_color="transparent")
        textos.pack(side="left", fill="both", expand=True)

        ctk.CTkLabel(
            textos, text=titulo, font=("Segoe UI", 14, "bold"), text_color=C_TEXTO,
            anchor="w", justify="left", wraplength=340,
        ).pack(fill="x")

        ctk.CTkLabel(
            textos, text=str(mensaje), font=("Segoe UI", 12), text_color=C_TEXTO_2,
            anchor="w", justify="left", wraplength=340,
        ).pack(fill="x", pady=(6, 0))

        fila = ctk.CTkFrame(marco, fg_color="transparent")
        fila.pack(fill="x", padx=18, pady=(10, 16))

        self._botones = []

        for i in reversed(range(len(botones))):

            es_principal = i == principal

            boton = ctk.CTkButton(
                fila,
                text=botones[i],
                width=96,
                height=34,
                corner_radius=8,
                font=("Segoe UI", 12, "bold" if es_principal else "normal"),
                fg_color=(C_ERROR if (es_principal and tipo == "error") else C_ACENTO) if es_principal else C_BOTON,
                hover_color=(C_ERROR_HOVER if (es_principal and tipo == "error") else C_ACENTO_HOVER) if es_principal else C_BOTON_HOVER,
                text_color="white" if es_principal else C_TEXTO,
                command=lambda n=i: self._elegir(n),
            )
            boton.pack(side="right", padx=(8, 0))
            self._botones.append(boton)

        self._indice_principal = principal

        self.bind("<Return>", lambda e: self._elegir(self._indice_principal))
        self.bind("<Escape>", lambda e: self._cerrar())
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        self.update_idletasks()

        try:
            esc = float(ctk.ScalingTracker.get_window_scaling(self))
        except Exception:
            esc = 1.0

        ancho = 460
        alto = max(170, int(self.winfo_reqheight() / esc) + 4)

        visible = False
        try:
            visible = parent is not None and parent.winfo_viewable()
        except Exception:
            pass

        centrar_ventana(self, ancho, alto, parent if visible else None)

        if visible:
            try:
                self.transient(parent)
            except Exception:
                pass

        self.deiconify()
        self.lift()

        try:
            self.after(50, self._tomar_foco)
        except Exception:
            pass

    def _tomar_foco(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _elegir(self, indice):
        self.resultado = indice
        self._salir()

    def _cerrar(self):
        self.resultado = self._cancelar
        self._salir()

    def _salir(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def _mostrar_mensaje(titulo, mensaje, parent=None, **kw):
    ventana = VentanaMensaje(parent, titulo, mensaje, **kw)
    try:
        ventana.wait_window()
    except Exception:
        pass
    return ventana.resultado


class _MensajesTema:
    """Mismas funciones que tkinter.messagebox, pero con el diseño de la app."""

    def showinfo(self, title="", message="", parent=None, **kw):
        _mostrar_mensaje(title, message, parent, tipo="info")
        return "ok"

    def showwarning(self, title="", message="", parent=None, **kw):
        _mostrar_mensaje(title, message, parent, tipo="aviso")
        return "ok"

    def showerror(self, title="", message="", parent=None, **kw):
        _mostrar_mensaje(title, message, parent, tipo="error")
        return "ok"

    def askyesno(self, title="", message="", parent=None, **kw):
        r = _mostrar_mensaje(
            title, message, parent,
            tipo="aviso" if kw.get("icon") == "warning" else "pregunta",
            botones=("Sí", "No"),
            principal=1 if kw.get("default") == "no" else 0,
            cancelar=1,
        )
        return r == 0

    def askyesnocancel(self, title="", message="", parent=None, **kw):
        r = _mostrar_mensaje(
            title, message, parent, tipo="pregunta", botones=("Sí", "No", "Cancelar"), principal=0, cancelar=2,
        )
        return True if r == 0 else (False if r == 1 else None)


messagebox = _MensajesTema()


class Planificador:
    """
    Cola de trabajos con dos límites:
      - cuántas canciones están BUSCANDO a la vez (max_busquedas), y
      - cuántas hay en proceso en total, buscando o descargando (max_en_proceso).
    Cuando un trabajo empieza a descargar llama a liberar() y deja su lugar de
    búsqueda a la siguiente canción, sin esperar a que termine la descarga.
    """

    def __init__(self, ejecutar, max_busquedas, max_en_proceso):
        self._ejecutar = ejecutar
        self._cola = collections.deque()
        self._cond = threading.Condition()

        self._max_busquedas = max(1, int(max_busquedas))
        self._max_en_proceso = max(self._max_busquedas, int(max_en_proceso))

        self._buscando = 0
        self._en_proceso = 0

        self._pausa = False

        threading.Thread(target=self._despachar, daemon=True).start()

    @property
    def pausado(self):
        return self._pausa

    def configurar(self, max_busquedas, max_en_proceso):
        """Cambia los límites en caliente (lo que ya está en marcha no se toca)."""
        with self._cond:
            self._max_busquedas = max(1, int(max_busquedas))
            self._max_en_proceso = max(self._max_busquedas, int(max_en_proceso))
            self._cond.notify_all()

    def pausar(self):
        """No arranca canciones nuevas (las que ya están en marcha siguen)."""
        with self._cond:
            self._pausa = True
            self._cond.notify_all()

    def reanudar(self):
        with self._cond:
            self._pausa = False
            self._cond.notify_all()

    def encolar(self, trabajo, urgente=False):
        with self._cond:
            if urgente:
                self._cola.appendleft(trabajo)
            else:
                self._cola.append(trabajo)
            self._cond.notify_all()

    def vaciar(self):
        """Descarta lo que todavía no empezó y lo devuelve."""
        with self._cond:
            descartados = list(self._cola)
            self._cola.clear()
            self._cond.notify_all()
        return descartados

    def _despachar(self):
        while True:
            with self._cond:
                while not (
                    self._cola
                    and not self._pausa
                    and self._buscando < self._max_busquedas
                    and self._en_proceso < self._max_en_proceso
                ):
                    self._cond.wait()

                trabajo = self._cola.popleft()
                self._buscando += 1
                self._en_proceso += 1

            threading.Thread(target=self._correr, args=(trabajo,), daemon=True).start()

    def _correr(self, trabajo):

        estado = {"liberado": False}

        def liberar():
            with self._cond:
                if estado["liberado"]:
                    return
                estado["liberado"] = True
                self._buscando -= 1
                self._cond.notify_all()

        try:
            self._ejecutar(trabajo, liberar)
        except Exception as e:
            print(f"[GUI] Error en un hilo de trabajo: {e}")
        finally:
            liberar()
            with self._cond:
                self._en_proceso -= 1
                self._cond.notify_all()


# ============================================================
# VENTANA DE LOGIN
# ============================================================

class VentanaInicio(ctk.CTkToplevel):
    """Pantalla de inicio: ventana propia, sin bordes y centrada. La principal espera detrás."""

    ANCHO = 460
    ALTO = 360

    def __init__(self, parent):
        super().__init__(parent)

        self.withdraw()
        self.overrideredirect(True)
        self.configure(fg_color=C_BORDE)

        # Marco con un borde fino de 1 px
        self.marco = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0)
        self.marco.pack(fill="both", expand=True, padx=1, pady=1)

        centro = ctk.CTkFrame(self.marco, fg_color="transparent")
        centro.place(relx=0.5, rely=0.5, anchor="center")

        logo = cargar_logo(120)
        if logo is not None:
            self._logo = logo
            ctk.CTkLabel(centro, image=logo, text="").pack(pady=(0, 10))

        ctk.CTkLabel(
            centro, text=NOMBRE_APP, font=("Segoe UI", 30, "bold"), text_color=C_TEXTO
        ).pack()

        ctk.CTkLabel(
            centro,
            text="Tus playlists de Spotify, en tu carpeta",
            font=("Segoe UI", 12),
            text_color=C_TEXTO_3,
        ).pack(pady=(0, 22))

        self.lbl_estado = ctk.CTkLabel(
            centro, text="Iniciando aplicación...", font=("Segoe UI", 13), text_color=C_TEXTO_2
        )
        self.lbl_estado.pack(pady=(0, 10))

        self.progress = ctk.CTkProgressBar(
            centro, width=300, height=6, fg_color=C_FONDO, progress_color=C_TEAL, corner_radius=3
        )
        self.progress.set(0)
        self.progress.pack()

        self.lbl_detalle = ctk.CTkLabel(
            centro,
            text="Preparando...",
            font=("Segoe UI", 10),
            text_color=C_TEXTO_3,
            wraplength=360,
            justify="center",
        )
        self.lbl_detalle.pack(pady=(10, 0))

        self.btn_cerrar = ctk.CTkButton(
            centro,
            text="Cerrar",
            width=120,
            height=32,
            corner_radius=8,
            fg_color=C_BOTON,
            hover_color=C_BOTON_HOVER,
            text_color=C_TEXTO,
            font=("Segoe UI", 12),
            command=self._cerrar_app,
        )

        ctk.CTkLabel(
            self.marco,
            text=f"v{actualizador.VERSION}",
            font=("Segoe UI", 10),
            text_color=C_TEXTO_4,
        ).place(relx=1.0, rely=1.0, anchor="se", x=-12, y=-8)

        poner_icono(self)
        centrar_ventana(self, self.ANCHO, self.ALTO)
        self.deiconify()

        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

    def mostrar_error(self, titulo, detalle):
        """Deja la ventana con un mensaje y un botón para cerrar la app."""

        try:
            self.attributes("-topmost", False)
        except Exception:
            pass

        self.lbl_estado.configure(text=titulo, text_color=C_ERROR)
        self.lbl_detalle.configure(text=detalle)
        self.progress.pack_forget()
        self.btn_cerrar.pack(pady=(16, 0))

    def _cerrar_app(self):
        try:
            self.master.destroy()
        except Exception:
            pass
        os._exit(0)


class VentanaConfigSoulseek(ctk.CTkToplevel):
    """Pide el usuario y la contraseña de Soulseek, crea slskd.yml y conecta."""

    ANCHO = 460
    ALTO = 560

    def __init__(self, parent, downloader, on_fin=None, primera_vez=False):
        super().__init__(parent)

        self.withdraw()

        self.downloader = downloader
        self.on_fin = on_fin
        self._terminado = False
        self._ocupado = False
        self._primera_vez = primera_vez

        self.title(f"{NOMBRE_APP} — Cuenta de Soulseek")
        self.resizable(False, False)
        self.configure(fg_color=C_FONDO)

        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        marco = ctk.CTkFrame(
            self, fg_color=C_PANEL, corner_radius=14, border_width=1, border_color=C_BORDE
        )
        marco.pack(padx=18, pady=18, fill="both", expand=True)

        logo = cargar_logo(84)
        if logo is not None:
            self._logo = logo
            ctk.CTkLabel(marco, image=logo, text="").pack(pady=(22, 6))

        titulo = f"Te damos la bienvenida a {NOMBRE_APP}" if primera_vez else "Cuenta de Soulseek"

        ctk.CTkLabel(
            marco, text=titulo, font=("Segoe UI", 18, "bold"), text_color=C_TEXTO
        ).pack(pady=(4, 4))

        ctk.CTkLabel(
            marco,
            text=(
                "Para buscar y descargar música, la app se conecta a Soulseek, "
                "una red gratuita.\nEscribe el usuario y la contraseña que quieras usar: "
                "si el usuario todavía no existe, Soulseek lo crea automáticamente.\n"
                "Los datos se guardan solo en este equipo."
            ),
            font=("Segoe UI", 11),
            text_color=C_TEXTO_2,
            justify="center",
            wraplength=380,
        ).pack(pady=(0, 14), padx=18)

        self.entry_user = ctk.CTkEntry(
            marco,
            placeholder_text="Usuario de Soulseek",
            height=40,
            corner_radius=8,
            fg_color=C_FONDO,
            border_color=C_BORDE,
            text_color=C_TEXTO,
            font=("Segoe UI", 12),
        )
        self.entry_user.pack(fill="x", padx=28, pady=4)

        self.entry_pass = ctk.CTkEntry(
            marco,
            placeholder_text="Contraseña",
            show="•",
            height=40,
            corner_radius=8,
            fg_color=C_FONDO,
            border_color=C_BORDE,
            text_color=C_TEXTO,
            font=("Segoe UI", 12),
        )
        self.entry_pass.pack(fill="x", padx=28, pady=4)
        self.entry_pass.bind("<Return>", lambda _e: self.guardar())
        self.entry_user.bind("<Return>", lambda _e: self.entry_pass.focus_set())

        self.var_ver = ctk.BooleanVar(value=False)
        self.chk_ver = ctk.CTkCheckBox(
            marco,
            text="Mostrar contraseña",
            variable=self.var_ver,
            command=lambda: self.entry_pass.configure(show="" if self.var_ver.get() else "•"),
            font=("Segoe UI", 11),
            text_color=C_TEXTO_2,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            border_color=C_BORDE,
            checkbox_width=18,
            checkbox_height=18,
        )
        self.chk_ver.pack(anchor="w", padx=30, pady=(4, 10))

        # Zona de estado: texto + barra de carga animada (spinner)
        self.zona_estado = ctk.CTkFrame(marco, fg_color="transparent", height=70)
        self.zona_estado.pack(fill="x", padx=28)
        self.zona_estado.pack_propagate(False)

        self.lbl_estado = ctk.CTkLabel(
            self.zona_estado,
            text="",
            font=("Segoe UI", 11),
            text_color=C_TEXTO_2,
            justify="center",
            wraplength=380,
        )
        self.lbl_estado.pack(pady=(4, 6))

        self.barra = ctk.CTkProgressBar(
            self.zona_estado,
            mode="indeterminate",
            height=6,
            corner_radius=3,
            fg_color=C_FONDO,
            progress_color=C_TEAL,
            indeterminate_speed=1.4,
        )

        self.btn_guardar = ctk.CTkButton(
            marco,
            text="Guardar y conectar",
            height=42,
            corner_radius=10,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            text_color="white",
            font=("Segoe UI", 13, "bold"),
            command=self.guardar,
        )
        self.btn_guardar.pack(fill="x", padx=28, pady=(8, 22))

        try:
            previo = self.downloader.usuario_soulseek_configurado()
            if previo:
                self.entry_user.insert(0, previo)
        except Exception:
            pass

        poner_icono(self)

        # Primera vez: la ventana principal está oculta, así que se centra en la
        # pantalla y no se ata a ella (si no, quedaría oculta también).
        if primera_vez:
            centrar_ventana(self, self.ANCHO, self.ALTO)
        else:
            self.transient(parent)
            centrar_ventana(self, self.ANCHO, self.ALTO, referencia=parent)

        self.deiconify()
        self.after(150, self._al_frente)

    def _al_frente(self):
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))
            self.focus_force()
            self.grab_set()
            (self.entry_pass if self.entry_user.get() else self.entry_user).focus_set()
        except Exception:
            pass

    def _poner_ocupado(self, ocupado, texto=""):
        self._ocupado = ocupado

        if ocupado:
            self.btn_guardar.configure(state="disabled", text="Conectando...")
            self.entry_user.configure(state="disabled")
            self.entry_pass.configure(state="disabled")
            self.lbl_estado.configure(text=texto, text_color=C_TEXTO_2)
            self.barra.pack(fill="x", pady=(0, 4))
            self.barra.start()
        else:
            self.barra.stop()
            self.barra.pack_forget()
            self.btn_guardar.configure(state="normal", text="Guardar y conectar")
            self.entry_user.configure(state="normal")
            self.entry_pass.configure(state="normal")

    def _cerrar(self):
        if self._ocupado:
            return
        self._terminar(False)

    def _terminar(self, ok):
        if self._terminado:
            return
        self._terminado = True
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        if self.on_fin:
            self.on_fin(ok)

    def guardar(self):
        if self._ocupado:
            return

        usuario = self.entry_user.get().strip()
        clave = self.entry_pass.get()

        if not usuario or not clave:
            self.lbl_estado.configure(
                text="Escribe el usuario y la contraseña.", text_color=C_AVISO
            )
            return

        self._poner_ocupado(
            True, "Guardando y conectando con Soulseek...\nPuede tardar hasta un minuto."
        )

        def tarea():
            try:
                ok, mensaje = self.downloader.aplicar_configuracion_soulseek(usuario, clave)
            except Exception as e:
                ok, mensaje = False, f"Error inesperado: {e}"
            try:
                self.after(0, lambda: self._resultado(ok, mensaje))
            except Exception:
                pass

        threading.Thread(target=tarea, daemon=True).start()

    def _resultado(self, ok, mensaje):
        self._poner_ocupado(False)

        if ok:
            self.btn_guardar.configure(state="disabled", text="Conectado")
            self.lbl_estado.configure(text="Conectado a Soulseek.", text_color=C_OK)
            self.after(700, lambda: self._terminar(True))
            return

        self.lbl_estado.configure(text=mensaje, text_color=C_ERROR)


class VentanaLogin(ctk.CTkToplevel):

    def __init__(self, parent, downloader, on_exito=None):
        super().__init__(parent)

        self.downloader = downloader
        self.on_exito = on_exito

        self.title("Iniciar sesión en slskd")
        poner_icono(self)
        self.geometry("360x280")
        self.resizable(False, False)
        self.configure(fg_color=C_FONDO)

        self.transient(parent)
        self.grab_set()

        self.frame = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=8)
        self.frame.pack(padx=16, pady=16, fill="both", expand=True)

        self.lbl_titulo = ctk.CTkLabel(
            self.frame,
            text="Acceso a slskd",
            font=("Segoe UI", 14, "bold"),
            text_color=C_TEXTO,
        )
        self.lbl_titulo.pack(pady=(12, 4))

        self.lbl_sub = ctk.CTkLabel(
            self.frame,
            text="Usuario y contraseña de la web de slskd (por defecto: slskd / slskd)",
            font=("Segoe UI", 10),
            text_color=C_TEXTO_2,
        )
        self.lbl_sub.pack(pady=(0, 12))

        self.entry_user = ctk.CTkEntry(
            self.frame,
            placeholder_text="Usuario",
            height=34,
            fg_color=C_FONDO,
            border_color=C_BOTON,
            text_color="white",
        )
        self.entry_user.pack(fill="x", padx=16, pady=4)

        self.entry_pass = ctk.CTkEntry(
            self.frame,
            placeholder_text="Contraseña",
            show="•",
            height=34,
            fg_color=C_FONDO,
            border_color=C_BOTON,
            text_color="white",
        )
        self.entry_pass.pack(fill="x", padx=16, pady=4)

        self.btn_guardar = ctk.CTkButton(
            self.frame,
            text="Guardar e Iniciar",
            height=34,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            font=("Segoe UI", 11, "bold"),
            command=self.guardar,
        )
        self.btn_guardar.pack(fill="x", padx=16, pady=(12, 8))

    def _restaurar_boton(self):
        self.btn_guardar.configure(state="normal", text="Guardar e Iniciar")

    def guardar(self):
        user = self.entry_user.get().strip()
        pas = self.entry_pass.get().strip()

        if not user or not pas:
            messagebox.showwarning(
                "Campos Incompletos",
                "Por favor completa el usuario y la contraseña.",
                parent=self,
            )
            return

        self.btn_guardar.configure(state="disabled", text="Conectando...")

        def tarea():
            try:
                servidor_ok = self.downloader.iniciar_servidor_slskd()

                # Si slskd solo está pidiendo el login, justamente para eso estamos aquí.
                if not servidor_ok and not self.downloader.requiere_login:
                    self.after(
                        0,
                        lambda: messagebox.showerror(
                            "Error", "No se pudo iniciar el servidor slskd.", parent=self
                        ),
                    )
                    self.after(0, self._restaurar_boton)
                    return

                exito = self.downloader.guardar_credenciales(user, pas)

                if exito:
                    self.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Éxito", "Sesión iniciada e integrada correctamente.", parent=self
                        ),
                    )
                    if self.on_exito:
                        self.after(0, self.on_exito)
                    self.after(0, self.destroy)
                else:
                    self.after(
                        0,
                        lambda: messagebox.showerror(
                            "Error",
                            "No se pudo conectar a Soulseek con esas credenciales.",
                            parent=self,
                        ),
                    )
                    self.after(0, self._restaurar_boton)

            except Exception as e:
                self.after(
                    0,
                    lambda err=e: messagebox.showerror("Error", str(err), parent=self),
                )
                self.after(0, self._restaurar_boton)

        threading.Thread(target=tarea, daemon=True).start()


# ============================================================
# VENTANA DE OPCIONES: resultados de slskd + búsqueda manual
# ============================================================

class VentanaResultados(ctk.CTkToplevel):
    """
    Muestra TODOS los archivos de audio que devolvió slskd (sin filtrar) y
    marca con «Sí» los que pasan el filtro automático. Permite buscar con otro
    texto y elegir a mano qué descargar.
    """

    COLUMNAS = (
        ("ok", "Filtro", 52, False),
        ("usuario", "Usuario", 130, False),
        ("archivo", "Archivo", 520, True),
        ("fmt", "Formato", 64, False),
        ("kbps", "kbps", 60, False),
        ("mb", "MB", 64, False),
        ("cola", "Cola", 96, False),
    )

    MAX_FILAS = 2000

    def __init__(self, app, card):
        super().__init__(app)

        self.app = app
        self.card = card
        self.downloader = app.downloader

        self.artista = card.cancion_data.get("artista", "")
        self.titulo = card.cancion_data.get("nombre", "")

        self.resultados = list(card.detalles.get("crudos") or [])
        self.pasan = set()
        self._filas = {}
        self._orden = (None, False)
        self._cola = queue.Queue()
        self.control_busqueda = ControlDescarga()
        self._buscando = False

        consulta_inicial = card.detalles.get("consulta") or f"{self.artista} - {self.titulo}"

        self.title(f"Opciones — {self.artista} - {self.titulo}")
        poner_icono(self)
        self.geometry("1060x620")
        self.minsize(820, 460)
        self.configure(fg_color=C_FONDO)
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        # ----------------------------------------------------
        # CABECERA
        # ----------------------------------------------------

        self.frame_top = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=8)
        self.frame_top.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkLabel(
            self.frame_top,
            text=f"{self.artista} — {self.titulo}",
            font=("Segoe UI", 14, "bold"),
            text_color=C_TEXTO,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 0))

        ctk.CTkLabel(
            self.frame_top,
            text=(
                "Estos son los archivos de audio que devolvió slskd, sin filtrar. "
                "Los marcados con «Sí» son los que pasan el filtro automático. "
                "Elige uno o varios (Ctrl/Shift): si eliges varios se piden a la vez "
                "y te quedas con el que responda primero."
            ),
            font=("Segoe UI", 10),
            text_color=C_TEXTO_2,
            anchor="w",
            justify="left",
            wraplength=980,
        ).pack(fill="x", padx=12, pady=(2, 6))

        # Búsqueda con texto libre

        self.frame_busqueda = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_busqueda.pack(fill="x", padx=12, pady=(0, 6))

        self.entry_consulta = ctk.CTkEntry(
            self.frame_busqueda,
            height=32,
            fg_color=C_FONDO,
            border_color=C_BOTON,
            text_color="white",
            placeholder_text="Texto a buscar en slskd...",
        )
        self.entry_consulta.insert(0, consulta_inicial)
        self.entry_consulta.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_consulta.bind("<Return>", lambda _e: self.buscar())

        self.btn_buscar = ctk.CTkButton(
            self.frame_busqueda,
            text="Buscar en slskd",
            image=icono_ctk("buscar", 14, "white"),
            compound="left",
            width=140,
            height=32,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            font=("Segoe UI", 11, "bold"),
            command=self.buscar,
        )
        self.btn_buscar.pack(side="right")

        # Filtros sobre la lista

        self.frame_filtro = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_filtro.pack(fill="x", padx=12, pady=(0, 10))

        self.entry_filtro = ctk.CTkEntry(
            self.frame_filtro,
            height=30,
            fg_color=C_FONDO,
            border_color=C_BOTON,
            text_color="white",
            placeholder_text="Filtrar esta lista (por ejemplo: extended 320 flac)",
        )
        self.entry_filtro.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.entry_filtro.bind("<KeyRelease>", lambda _e: self._poblar())

        self.var_solo_ok = tk.BooleanVar(value=False)

        self.chk_solo_ok = ctk.CTkCheckBox(
            self.frame_filtro,
            text="Solo los que pasan el filtro automático",
            variable=self.var_solo_ok,
            command=self._poblar,
            font=("Segoe UI", 11),
            text_color=C_TEXTO,
        )
        self.chk_solo_ok.pack(side="right")

        # ----------------------------------------------------
        # TABLA
        # ----------------------------------------------------

        self.frame_tabla = ctk.CTkFrame(self, fg_color=C_FONDO)
        self.frame_tabla.pack(fill="both", expand=True, padx=12, pady=6)

        estilo = ttk.Style(self)

        try:
            estilo.theme_use("clam")
        except Exception:
            pass

        estilo.configure(
            "Res.Treeview",
            background=C_FONDO,
            fieldbackground=C_FONDO,
            foreground=C_TEXTO,
            rowheight=24,
            borderwidth=0,
        )
        estilo.configure(
            "Res.Treeview.Heading",
            background=C_PANEL,
            foreground=C_TEXTO,
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )
        estilo.map(
            "Res.Treeview",
            background=[("selected", C_ACENTO)],
            foreground=[("selected", "#ffffff")],
        )
        estilo.map("Res.Treeview.Heading", background=[("active", C_BOTON)])

        self.tree = ttk.Treeview(
            self.frame_tabla,
            columns=[c[0] for c in self.COLUMNAS],
            show="headings",
            style="Res.Treeview",
            selectmode="extended",
        )

        for clave, texto, ancho, estirar in self.COLUMNAS:
            self.tree.heading(clave, text=texto, command=lambda c=clave: self._ordenar_por(c))
            self.tree.column(
                clave,
                width=ancho,
                minwidth=ancho if estirar else 40,
                stretch=estirar,
                anchor="center" if clave in ("ok", "fmt", "kbps", "mb", "cola") else "w",
            )

        self.tree.tag_configure("ok", foreground=C_OK)
        self.tree.tag_configure("bloqueado", foreground=C_TEXTO_4)

        estilo_barra = dict(
            fg_color=C_FONDO_PROFUNDO,
            button_color="#4b5170",
            button_hover_color=C_ACENTO,
        )

        self.scroll = ctk.CTkScrollbar(
            self.frame_tabla, command=self.tree.yview, width=16, **estilo_barra
        )
        self.scroll_x = ctk.CTkScrollbar(
            self.frame_tabla, command=self.tree.xview, orientation="horizontal", height=16, **estilo_barra
        )

        self.tree.configure(yscrollcommand=self.scroll.set, xscrollcommand=self.scroll_x.set)

        self.frame_tabla.grid_rowconfigure(0, weight=1)
        self.frame_tabla.grid_columnconfigure(0, weight=1)

        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.scroll_x.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        self.tree.bind("<Shift-MouseWheel>", lambda e: self.tree.xview_scroll(-1 if e.delta > 0 else 1, "units"))

        self.tree.bind("<Double-1>", lambda _e: self.descargar())

        # ----------------------------------------------------
        # BARRA INFERIOR
        # ----------------------------------------------------

        self.frame_bottom = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=8)
        self.frame_bottom.pack(fill="x", padx=12, pady=(6, 12))

        self.lbl_estado = ctk.CTkLabel(
            self.frame_bottom,
            text="",
            font=("Segoe UI", 10),
            text_color=C_TEXTO_2,
            anchor="w",
        )
        self.lbl_estado.pack(side="left", fill="x", expand=True, padx=12, pady=10)

        self.btn_cerrar = ctk.CTkButton(
            self.frame_bottom,
            text="Cerrar",
            width=90,
            height=32,
            fg_color=C_BOTON,
            hover_color=C_BOTON_HOVER,
            command=self._cerrar,
        )
        self.btn_cerrar.pack(side="right", padx=(6, 12), pady=8)

        self.btn_descargar = ctk.CTkButton(
            self.frame_bottom,
            text="Descargar seleccionados",
            image=icono_ctk("descargar", 14, "white"),
            compound="left",
            width=190,
            height=32,
            fg_color=C_CTA,
            hover_color=C_CTA_HOVER,
            font=("Segoe UI", 11, "bold"),
            command=self.descargar,
        )
        self.btn_descargar.pack(side="right", pady=8)

        # ----------------------------------------------------
        # DATOS INICIALES
        # ----------------------------------------------------

        self._recalcular_filtro_automatico()
        self._poblar()

        if self.resultados:
            self.lbl_estado.configure(text=self._texto_resumen(consulta_inicial))
        else:
            self.lbl_estado.configure(
                text=(
                    "La última búsqueda no dejó archivos de audio legibles. "
                    "Prueba con «Buscar en slskd»."
                )
            )

        self.after(200, self._sondear)
        self.after(250, self.lift)
        self.after(300, self.focus)

    # ========================================================
    # DATOS
    # ========================================================

    @staticmethod
    def _clave(resultado):
        return (
            str(resultado.get("username", "")).lower().strip(),
            str(resultado.get("filename", "")).replace("\\", "/").lower().strip(),
        )

    def _recalcular_filtro_automatico(self):
        try:
            validos = self.downloader.rankear_resultados(
                self.resultados,
                self.artista,
                self.titulo,
                limite=None,
            )
            self.pasan = {self._clave(r) for r in validos}

        except Exception as e:
            print(f"[GUI] No se pudo calcular el filtro automático: {e}")
            self.pasan = set()

    def _texto_resumen(self, consulta):
        return (
            f"{len(self.resultados)} archivos de audio de «{consulta}»  ·  "
            f"{len(self.pasan)} pasan el filtro automático (marcados con «Sí»)"
        )

    # ========================================================
    # TABLA
    # ========================================================

    def _filas_visibles(self):
        palabras = self.entry_filtro.get().strip().lower().split()
        solo_ok = self.var_solo_ok.get()

        salida = []

        for r in self.resultados:

            if solo_ok and self._clave(r) not in self.pasan:
                continue

            texto = f"{r.get('username', '')} {r.get('filename', '')}".lower()

            if palabras and not all(p in texto for p in palabras):
                continue

            salida.append(r)

        return salida

    def _claves_orden(self):
        return {
            "ok": lambda r: 0 if self._clave(r) in self.pasan else 1,
            "usuario": lambda r: str(r.get("username", "")).lower(),
            "archivo": lambda r: str(r.get("filename", "")).lower(),
            "fmt": lambda r: str(r.get("extension", "")),
            "kbps": lambda r: float(r.get("bitrate") or 0),
            "mb": lambda r: float(r.get("size") or 0),
            "cola": lambda r: (
                0 if r.get("hasFreeUploadSlot") else 1,
                float(r.get("queueLength") or 0),
            ),
        }

    def _ordenar_por(self, columna):
        actual, desc = self._orden

        if actual == columna:
            self._orden = (columna, not desc)
        else:
            # Para kbps y MB lo natural es de mayor a menor.
            self._orden = (columna, columna in ("kbps", "mb"))

        self._poblar()

    def _poblar(self):
        filas = self._filas_visibles()

        columna, desc = self._orden

        if columna is None:
            # Orden por defecto: primero los que pasan el filtro, luego mejor bitrate.
            filas.sort(
                key=lambda r: (
                    0 if self._clave(r) in self.pasan else 1,
                    -float(r.get("bitrate") or 0),
                    str(r.get("username", "")).lower(),
                )
            )
        else:
            filas.sort(key=self._claves_orden()[columna], reverse=desc)

        self.tree.delete(*self.tree.get_children())
        self._filas.clear()

        for i, r in enumerate(filas[: self.MAX_FILAS]):

            bitrate = float(r.get("bitrate") or 0)
            size = float(r.get("size") or 0)

            if r.get("locked"):
                cola = "bloqueado"
            elif r.get("hasFreeUploadSlot"):
                cola = "libre"
            else:
                cola = f"cola {int(float(r.get('queueLength') or 0))}"

            pasa = self._clave(r) in self.pasan

            etiquetas = []

            if pasa:
                etiquetas.append("ok")

            if r.get("locked"):
                etiquetas.append("bloqueado")

            iid = str(i)

            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "Sí" if pasa else "",
                    r.get("username", ""),
                    str(r.get("filename", "")).replace("\\", " / "),
                    str(r.get("extension", "")).lstrip(".").upper(),
                    f"{bitrate:.0f}" if bitrate > 0 else "-",
                    f"{size / 1048576:.1f}" if size > 0 else "-",
                    cola,
                ),
                tags=tuple(etiquetas),
            )

            self._filas[iid] = r

        if len(filas) > self.MAX_FILAS:
            self.lbl_estado.configure(
                text=(
                    f"Mostrando {self.MAX_FILAS} de {len(filas)} resultados. "
                    "Usa el filtro de texto para acotar."
                )
            )

    # ========================================================
    # BÚSQUEDA MANUAL
    # ========================================================

    def buscar(self):
        if self._buscando:
            return

        consulta = self.entry_consulta.get().strip()

        if not consulta:
            return

        self._buscando = True
        self.btn_buscar.configure(state="disabled", text="Buscando...")
        self.lbl_estado.configure(text="Creando la búsqueda...")

        self.control_busqueda = ControlDescarga()

        control = self.control_busqueda

        def tarea():
            try:
                crudos = self.downloader.buscar_manual(
                    consulta,
                    control=control,
                    progreso=lambda texto: self._cola.put(("estado", texto)),
                )

                if crudos is None:
                    self._cola.put(("error", "slskd no pudo crear la búsqueda."))
                else:
                    self._cola.put(("resultados", crudos, consulta))

            except Exception as e:
                self._cola.put(("error", str(e)))

        threading.Thread(target=tarea, daemon=True).start()

    def _sondear(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        try:
            while True:

                try:
                    msg = self._cola.get_nowait()
                except queue.Empty:
                    break

                self._procesar_mensaje(msg)

        except Exception as e:
            print(f"[GUI] Error en la ventana de opciones: {e}")

        finally:
            try:
                self.after(200, self._sondear)
            except Exception:
                pass

    def _procesar_mensaje(self, msg):
        tipo = msg[0]

        if tipo == "estado":
            self.lbl_estado.configure(text=msg[1])
            return

        self._buscando = False
        self.btn_buscar.configure(state="normal", text="Buscar en slskd")

        if tipo == "error":
            self.lbl_estado.configure(text=f"No se pudo buscar: {msg[1]}")
            return

        if tipo == "resultados":

            crudos, consulta = msg[1], msg[2]

            self.resultados = list(crudos or [])

            # Se guardan en la tarjeta para volver a verlos más tarde.
            self.card.detalles["crudos"] = self.resultados
            self.card.detalles["consulta"] = consulta

            self._recalcular_filtro_automatico()
            self._poblar()

            if self.resultados:
                self.lbl_estado.configure(text=self._texto_resumen(consulta))
            else:
                self.lbl_estado.configure(
                    text=f"La búsqueda «{consulta}» no devolvió archivos de audio."
                )

    # ========================================================
    # DESCARGAR
    # ========================================================

    def descargar(self):
        seleccion = self.tree.selection()

        if not seleccion:
            messagebox.showinfo(
                "Elige un archivo",
                "Selecciona uno o varios archivos de la lista.",
                parent=self,
            )
            return

        seleccion = sorted(seleccion, key=lambda iid: self.tree.index(iid))

        elegidos = [self._filas[i] for i in seleccion if i in self._filas]

        descargables = [r for r in elegidos if not r.get("locked")]

        if not descargables:
            messagebox.showwarning(
                "Archivos bloqueados",
                "Los archivos elegidos están bloqueados por su dueño y no se pueden descargar.",
                parent=self,
            )
            return

        self.app.descargar_elegidos(self.card, descargables)

        self._cerrar()

    def _cerrar(self):
        try:
            self.control_busqueda.cancelar.set()
        except Exception:
            pass

        self.destroy()


# ============================================================
# ITEM DE CANCIÓN (los datos de una fila; se dibuja en la lista virtual)
# ============================================================

class VentanaAjustes(ctk.CTkToplevel):
    """Configuración de la app."""

    def __init__(self, app, valores=None):
        super().__init__(app)

        self.app = app
        self.downloader = app.downloader

        self.title(f"Ajustes — {NOMBRE_APP}")
        poner_icono(self)
        self.configure(fg_color=C_FONDO)
        self.minsize(560, 520)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.vars = {}
        self._etiquetas = {}

        actuales = dict(valores) if valores else dict(self.downloader.ajustes.datos)

        # ---- botones de abajo (se crean primero para que siempre queden visibles)
        barra = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0)
        barra.pack(side="bottom", fill="x")

        ctk.CTkButton(
            barra, text="Restablecer", width=110, height=34, corner_radius=8,
            fg_color="transparent", border_width=1, border_color=C_BORDE,
            hover_color=C_BOTON, text_color=C_TEXTO_2, font=("Segoe UI", 12),
            command=self._restablecer,
        ).pack(side="left", padx=(16, 0), pady=12)

        ctk.CTkButton(
            barra, text="Guardar", width=110, height=34, corner_radius=8,
            fg_color=C_ACENTO, hover_color=C_ACENTO_HOVER, text_color="white",
            font=("Segoe UI", 12, "bold"), command=self._guardar,
        ).pack(side="right", padx=(8, 16), pady=12)

        ctk.CTkButton(
            barra, text="Cancelar", width=96, height=34, corner_radius=8,
            fg_color=C_BOTON, hover_color=C_BOTON_HOVER, text_color=C_TEXTO,
            font=("Segoe UI", 12), command=self.destroy,
        ).pack(side="right", pady=12)

        self.cuerpo = ctk.CTkScrollableFrame(
            self, fg_color=C_FONDO, corner_radius=0,
            scrollbar_button_color="#4b5170", scrollbar_button_hover_color=C_ACENTO,
        )
        self.cuerpo.pack(fill="both", expand=True, padx=(10, 4), pady=(8, 0))

        # ================= DESCARGAS =================
        self._seccion("Descargas")

        tarjeta = self._tarjeta()

        ctk.CTkLabel(
            tarjeta, text="Carpeta de descargas", font=("Segoe UI", 12, "bold"),
            text_color=C_TEXTO, anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 0))

        ctk.CTkLabel(
            tarjeta,
            text="Ahí queda solo el archivo de audio. Los cambios valen para las próximas descargas.",
            font=("Segoe UI", 10), text_color=C_TEXTO_3, anchor="w", justify="left", wraplength=470,
        ).pack(fill="x", padx=14)

        fila = ctk.CTkFrame(tarjeta, fg_color="transparent")
        fila.pack(fill="x", padx=14, pady=(6, 12))

        self.var_carpeta = tk.StringVar(value=actuales["carpeta_descargas"])

        self.entry_carpeta = ctk.CTkEntry(
            fila, height=32, fg_color=C_FONDO, border_color=C_BORDE, text_color=C_TEXTO,
            font=("Segoe UI", 11),
        )
        self.entry_carpeta.pack(side="left", fill="x", expand=True, padx=(0, 8))

        ctk.CTkButton(
            fila, text="Cambiar...", image=icono_ctk("carpeta", 14, "white"), compound="left",
            width=104, height=32, corner_radius=8, fg_color=C_BOTON, hover_color=C_BOTON_HOVER,
            text_color="white", font=("Segoe UI", 11), command=self._elegir_carpeta,
        ).pack(side="left")

        self._mostrar_carpeta()

        tarjeta = self._tarjeta()

        derecha = self._fila(tarjeta, "Nombre del archivo", "Cómo se llama el audio que queda en la carpeta.")

        self.var_formato = tk.StringVar(value=actuales["formato_nombre"])

        self._nombres_formato = {
            "titulo_artista": "Canción - Artista",
            "artista_titulo": "Artista - Canción",
        }

        self.seg_formato = ctk.CTkSegmentedButton(
            derecha,
            values=list(self._nombres_formato.values()),
            command=self._cambio_formato,
            fg_color=C_FONDO, selected_color=C_ACENTO, selected_hover_color=C_ACENTO_HOVER,
            unselected_color=C_BOTON, unselected_hover_color=C_BOTON_HOVER,
            font=("Segoe UI", 11),
        )
        self.seg_formato.pack()
        self.seg_formato.set(self._nombres_formato[actuales["formato_nombre"]])

        self.lbl_ejemplo = ctk.CTkLabel(
            tarjeta, text="", font=("Segoe UI", 10), text_color=C_TEXTO_3, anchor="w",
        )
        self.lbl_ejemplo.pack(fill="x", padx=14, pady=(0, 10))
        self._cambio_formato()

        tarjeta = self._tarjeta()
        self._interruptor(
            tarjeta, "abrir_carpeta_al_terminar", "Abrir la carpeta al terminar la lista",
            "Se abre el explorador cuando se procesan todas las canciones.", actuales,
        )

        # ================= TIEMPOS =================
        self._seccion("Tiempos de espera")

        tarjeta = self._tarjeta()
        self._deslizador(
            tarjeta, "seg_sin_arranque", "Si un match no empieza a descargar",
            "Segundos de espera antes de pasar al siguiente candidato (por ejemplo, si está en una cola remota).",
            10, 300, 5, "s", actuales,
        )
        self._deslizador(
            tarjeta, "seg_sin_progreso", "Si la descarga se tranca",
            "Segundos sin avanzar antes de descartar el match y probar otro.",
            10, 180, 5, "s", actuales,
        )
        self._deslizador(
            tarjeta, "espera_busqueda", "Duración de la búsqueda",
            "Cuánto escucha respuestas de Soulseek por cada canción. Más tiempo = más resultados, pero más lento.",
            8, 60, 1, "s", actuales,
        )

        # ================= RENDIMIENTO =================
        self._seccion("Rendimiento")

        tarjeta = self._tarjeta()
        self._deslizador(
            tarjeta, "max_simultaneas", "Canciones buscando a la vez",
            "Cuántas canciones se buscan al mismo tiempo. Se aplica enseguida.",
            1, 12, 1, "", actuales,
        )
        self._deslizador(
            tarjeta, "candidatos_en_carrera", "Matches que compiten por canción",
            "Se piden varios y gana el primero que empieza a transferir. 1 = de a uno.",
            1, 6, 1, "", actuales,
        )

        # ================= CALIDAD Y FUENTES =================
        self._seccion("Calidad y fuentes")

        tarjeta = self._tarjeta()
        self._interruptor(
            tarjeta, "preferir_extended", "Preferir Extended Mix",
            "Entre varios matches, se prefieren las versiones extendidas.", actuales,
        )
        self._interruptor(
            tarjeta, "youtube_respaldo", "Probar YouTube Music si no está en Soulseek",
            "Es lo mismo que la casilla de la ventana principal.", actuales,
        )

        derecha = self._fila(
            tarjeta, "Calidad del MP3 de YouTube",
            "YouTube no tiene audio de 320 kbps reales; esto es solo cómo se codifica el MP3.",
        )

        self.var_bitrate = tk.StringVar(value=f"{actuales['youtube_bitrate']} kbps")

        ctk.CTkOptionMenu(
            derecha, values=[f"{b} kbps" for b in BITRATES_YOUTUBE], variable=self.var_bitrate,
            width=110, height=30, fg_color=C_BOTON, button_color=C_BOTON_HOVER,
            button_hover_color=C_ACENTO, dropdown_fg_color=C_PANEL, dropdown_hover_color=C_ACENTO,
            dropdown_text_color=C_TEXTO, text_color=C_TEXTO, font=("Segoe UI", 11),
        ).pack()

        # ================= APLICACIÓN =================
        self._seccion("Aplicación")

        tarjeta = self._tarjeta()
        self._interruptor(
            tarjeta, "confirmar_cierre", "Avisar antes de cerrar con descargas en curso",
            "Si lo apagas, la app cierra sin preguntar (las descargas se detienen igual).", actuales,
        )
        self._interruptor(
            tarjeta, "buscar_actualizaciones", "Buscar actualizaciones al abrir",
            "Comprueba si hay una versión nueva cada vez que inicias la app.", actuales,
        )

        centrar_ventana(self, 640, 720, app)

        self.transient(app)
        self.after(120, self._tomar_foco)

    # ---------- piezas ----------

    def _tomar_foco(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _seccion(self, titulo):
        ctk.CTkLabel(
            self.cuerpo, text=titulo.upper(), font=("Segoe UI", 10, "bold"),
            text_color=C_ACENTO, anchor="w",
        ).pack(fill="x", padx=6, pady=(14, 4))

    def _tarjeta(self):
        t = ctk.CTkFrame(
            self.cuerpo, fg_color=C_PANEL, corner_radius=10, border_width=1, border_color=C_BORDE
        )
        t.pack(fill="x", padx=4, pady=(0, 6))
        return t

    def _fila(self, tarjeta, titulo, descripcion):
        """Fila con texto a la izquierda; devuelve el contenedor de la derecha."""

        fila = ctk.CTkFrame(tarjeta, fg_color="transparent")
        fila.pack(fill="x", padx=14, pady=(10, 6))

        textos = ctk.CTkFrame(fila, fg_color="transparent")
        textos.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            textos, text=titulo, font=("Segoe UI", 12, "bold"), text_color=C_TEXTO, anchor="w",
        ).pack(fill="x")

        ctk.CTkLabel(
            textos, text=descripcion, font=("Segoe UI", 10), text_color=C_TEXTO_3,
            anchor="w", justify="left", wraplength=300,
        ).pack(fill="x")

        derecha = ctk.CTkFrame(fila, fg_color="transparent")
        derecha.pack(side="right", padx=(12, 0))

        return derecha

    def _interruptor(self, tarjeta, clave, titulo, descripcion, actuales):

        derecha = self._fila(tarjeta, titulo, descripcion)

        var = tk.BooleanVar(value=bool(actuales[clave]))
        self.vars[clave] = var

        ctk.CTkSwitch(
            derecha, text="", variable=var, width=44, progress_color=C_ACENTO,
            button_color="white", fg_color=C_BOTON,
        ).pack()

    def _deslizador(self, tarjeta, clave, titulo, descripcion, minimo, maximo, paso, unidad, actuales):

        derecha = self._fila(tarjeta, titulo, descripcion)

        var = tk.IntVar(value=int(max(minimo, min(maximo, actuales[clave]))))
        self.vars[clave] = var

        etiqueta = ctk.CTkLabel(
            derecha, text="", width=54, font=("Segoe UI", 12, "bold"), text_color=C_TEXTO, anchor="e",
        )

        def cambio(valor):
            valor = int(round(float(valor) / paso) * paso)
            var.set(valor)
            etiqueta.configure(text=f"{valor} {unidad}".strip())

        slider = ctk.CTkSlider(
            derecha, from_=minimo, to=maximo, number_of_steps=max(1, (maximo - minimo) // paso),
            width=170, command=cambio, progress_color=C_ACENTO, button_color=C_ACENTO,
            button_hover_color=C_ACENTO_HOVER, fg_color=C_BOTON,
        )
        slider.set(var.get())
        slider.pack(side="left", padx=(0, 8))
        etiqueta.pack(side="left")

        cambio(var.get())

    def _mostrar_carpeta(self):

        texto = self.var_carpeta.get().strip()

        if not texto:
            texto = str(self.downloader.carpeta_predeterminada()) + "   (predeterminada)"

        self.entry_carpeta.configure(state="normal")
        self.entry_carpeta.delete(0, "end")
        self.entry_carpeta.insert(0, texto)
        self.entry_carpeta.configure(state="disabled")

    def _elegir_carpeta(self):

        inicial = self.var_carpeta.get().strip() or str(self.downloader.download_dir)

        try:
            elegida = filedialog.askdirectory(parent=self, initialdir=inicial, title="Carpeta de descargas")
        except Exception:
            elegida = ""

        if elegida:
            self.var_carpeta.set(os.path.normpath(elegida))
            self._mostrar_carpeta()

        self._tomar_foco()

    def _cambio_formato(self, _valor=None):

        elegido = self.seg_formato.get()

        for clave, texto in self._nombres_formato.items():
            if texto == elegido:
                self.var_formato.set(clave)

        self.lbl_ejemplo.configure(
            text="Ejemplo:  " + construir_nombre("AC/DC", "Back In Black", self.var_formato.get()) + ".mp3"
        )

    def _restablecer(self):

        if not messagebox.askyesno(
            "Restablecer ajustes",
            "Todos los ajustes vuelven a sus valores originales (después tienes que pulsar Guardar). ¿Continuar?",
            parent=self,
        ):
            return

        # Se vuelve a abrir la ventana con los valores originales.
        self.destroy()
        self.app.abrir_ajustes(valores=dict(AJUSTES_PREDETERMINADOS))

    def _guardar(self):

        nuevos = {k: v.get() for k, v in self.vars.items()}

        nuevos["carpeta_descargas"] = self.var_carpeta.get().strip()
        nuevos["formato_nombre"] = self.var_formato.get()

        try:
            nuevos["youtube_bitrate"] = int(self.var_bitrate.get().split()[0])
        except Exception:
            nuevos["youtube_bitrate"] = AJUSTES_PREDETERMINADOS["youtube_bitrate"]

        self.app.guardar_ajustes(nuevos, ventana=self)


class ItemCancion:
    """
    Una canción de la lista. No es un widget: solo guarda sus datos y lo que
    hay que dibujar. La ListaVirtual la dibuja únicamente cuando está a la vista.
    """

    def __init__(self, cancion_data, lista):
        self.cancion_data = cancion_data
        self.lista = lista

        # Señales hacia el downloader (cancelar / siguiente match)
        self.control = ControlDescarga()

        # Grupo de la canción: pendientes / proceso / completadas / atencion
        self.seccion = None

        # Cada vez que se (re)encola la canción sube el número; sirve para
        # ignorar avisos de intentos viejos.
        self.token = 0

        # Lo que devolvió la última búsqueda (para la ventana de opciones)
        self.detalles = {}

        # Archivos elegidos a mano (si no es None, no se busca: se descargan)
        self.manual = None

        # "youtube" = ir directo a YouTube Music (sin buscar en Soulseek)
        self.fuente = None

        # Texto para el buscador de la lista
        self.texto_busqueda = (
            f"{cancion_data.get('nombre', '')} "
            f"{cancion_data.get('artista', '')} "
            f"{cancion_data.get('album', '')}"
        ).lower()

        # Lo que se dibuja en la fila
        self.texto_estado = "Pendiente"
        self.color = C_TEXTO_3
        self.encolada = False  # tiene un trabajo esperando en la fila
        self.modo = "estado"  # estado = solo texto, progreso = barra + botones, accion = botones
        self.detalle = ""
        self.progreso_actual = 0.0
        self.progreso_objetivo = 0.0

    # ========================================================
    # AUXILIARES
    # ========================================================

    def _cambiado(self):
        self.lista.refrescar_item(self)

    def _mostrar_texto(self, texto, color, modo="estado"):
        self.texto_estado = texto
        self.color = color
        self.modo = modo
        self._cambiado()

    def reiniciar_visual(self):
        """Deja la fila lista para un nuevo intento."""
        self.progreso_actual = 0.0
        self.progreso_objetivo = 0.0
        self.detalle = ""
        self._cambiado()

    def mostrar_detalle(self, texto):
        self.detalle = str(texto or "").strip()
        self._cambiado()

    # ========================================================
    # ESTADOS
    # ========================================================

    def mostrar_pendiente(self, texto="En espera..."):
        self._mostrar_texto(texto, C_AVISO)
        self.cancion_data["estado"] = "Pendiente"

    def mostrar_busqueda(self, texto="Buscando match..."):
        self._mostrar_texto(texto, C_AVISO)

    def actualizar_descarga(self, datos):

        # Si la fila ya cambió de grupo, no se toca.
        if self.seccion != "proceso":
            return

        datos = datos or {}

        estado = str(datos.get("estado") or "").lower().replace(" ", "")
        intento = datos.get("intento")
        total = datos.get("total")

        sufijo = f" ({intento}/{total})" if intento and total and total > 1 else ""

        if "inprogress" in estado:
            texto, color = f"Descargando{sufijo}", C_DESCARGANDO
        elif "queued" in estado:
            texto, color = f"En cola{sufijo}", C_AVISO
        else:
            texto, color = f"Solicitando{sufijo}", C_AVISO

        try:
            valor = float(datos.get("progreso") or 0) / 100.0
        except (TypeError, ValueError):
            valor = 0.0

        self.progreso_objetivo = max(0.0, min(1.0, valor))

        self.texto_estado = texto
        self.color = color
        self.modo = "progreso"
        self.cancion_data["estado"] = "Descargando"

        self._cambiado()

        # La barra avanza suavemente hasta el valor nuevo.
        self.lista.animar()

    def marcar_completado(self, detalle=""):
        self.progreso_actual = 1.0
        self.progreso_objetivo = 1.0
        self._mostrar_texto("Completado", C_OK)
        self.mostrar_detalle(detalle)
        self.cancion_data["estado"] = "Completado"

    def marcar_existente(self, detalle=""):
        self._mostrar_texto("Ya descargada", C_AVISO)
        self.mostrar_detalle(detalle)
        self.cancion_data["estado"] = "Ya descargada"

    def marcar_no_encontrado(self, detalle=None):
        self._mostrar_texto("Sin match", C_ERROR, "accion")

        if detalle is not None:
            self.mostrar_detalle(detalle)

        self.cancion_data["estado"] = "No encontrado"

    def marcar_error(self, texto="Error", detalle=None):
        self._mostrar_texto(texto, C_ERROR, "accion")

        if detalle is not None:
            self.mostrar_detalle(detalle)

        self.cancion_data["estado"] = "Error"

    def marcar_cancelado(self):
        self._mostrar_texto("Cancelado", C_ERROR, "accion")
        self.mostrar_detalle("")
        self.cancion_data["estado"] = "Cancelado"


# ============================================================
# LISTA VIRTUAL
# ============================================================

def menu_tema(master):
    """Menú contextual con los colores de la app."""

    return tk.Menu(
        master,
        tearoff=0,
        bg=C_PANEL,
        fg=C_TEXTO,
        activebackground=C_ACENTO,
        activeforeground="white",
        disabledforeground=C_TEXTO_4,
        bd=0,
        relief="flat",
    )


class ListaVirtual(tk.Frame):
    """
    Lista de filas dibujada en UN solo canvas. Solo se dibujan las filas que se
    ven en pantalla (unas 25), sin importar cuántas canciones haya. Por eso
    redimensionar la ventana o hacer scroll es rápido aunque la lista sea larga.
    """

    FONDO = C_FONDO
    FILA = C_FILA
    FILA_HOVER = C_FILA_HOVER

    def __init__(self, master, acciones, mensaje_vacio=""):
        super().__init__(master, bg=self.FONDO, highlightthickness=0, bd=0)

        # acciones: {"reintentar": f, "opciones": f, "cancelar": f, "siguiente": f, "eliminar": f}
        self.acciones = acciones
        self.mensaje_vacio = mensaje_vacio

        self.items = []
        self._indice = {}
        self.offset = 0

        self._ancho = 0
        self._alto = 0

        self._redibujo_id = None
        self._anim_id = None
        self._necesita_anim = False

        self._hits = []
        self._hover_item = None
        self._hover_hit = None

        # Selección múltiple (clic, Ctrl+clic y Mayús+clic)
        self.seleccion = set()
        self._ancla = None
        self.al_cambiar_seleccion = None
        self.menu_lote = None

        self._tt_id = None
        self._tt_ventana = None
        self._tt_pos = (0, 0)

        self._cache_texto = {}
        self._fotos = {}

        # Escala de pantalla (por ejemplo 1.5 con Windows al 150%).
        try:
            self.esc = max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self.esc = 1.0

        e = self.e

        self.alto_fila = e(34)

        # Los tamaños van en píxeles (negativos), igual que en el resto de la app.
        self.f_titulo = tkfont.Font(family="Segoe UI", size=-e(11), weight="bold")
        self.f_artista = tkfont.Font(family="Segoe UI", size=-e(10))
        self.f_estado = tkfont.Font(family="Segoe UI", size=-e(10), weight="bold")
        self.f_detalle = tkfont.Font(family="Segoe UI", size=-e(9))
        self.f_boton = tkfont.Font(family="Segoe UI", size=-e(11), weight="bold")
        self.f_vacio = tkfont.Font(family="Segoe UI", size=-e(12))

        self.canvas = tk.Canvas(
            self,
            bg=self.FONDO,
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )

        self.scroll = ctk.CTkScrollbar(
            self,
            command=self._yview,
            fg_color=self.FONDO,
            button_color=C_PANEL,
            button_hover_color=C_BOTON,
        )

        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_doble_click)
        self.canvas.bind("<Button-3>", self._on_menu)

        # La rueda del mouse se escucha en toda la ventana, pero solo actúa
        # cuando el puntero está sobre la lista.
        self.bind_all("<MouseWheel>", self._on_rueda, add="+")
        self.bind_all("<Button-4>", self._on_rueda, add="+")
        self.bind_all("<Button-5>", self._on_rueda, add="+")

        self._calcular_columnas()

    # ========================================================
    # UTILIDADES
    # ========================================================

    def e(self, valor):
        """Convierte píxeles 'normales' a píxeles de esta pantalla."""
        return int(round(valor * self.esc))

    def _calcular_columnas(self):
        e = self.e

        ancho = max(self._ancho, 1)

        self._col_titulo = max(e(130), min(e(340), int(ancho * 0.25)))
        self._col_artista = max(e(90), min(e(220), int(ancho * 0.15)))
        self._col_estado = e(128)

        separacion = e(8)

        self._x_titulo = e(14)
        self._x_artista = self._x_titulo + self._col_titulo + separacion
        self._x_estado = self._x_artista + self._col_artista + separacion
        self._x_detalle = self._x_estado + self._col_estado + separacion

    def _ajustar(self, texto, ancho, fuente):
        """Recorta el texto con «…» para que quepa en el ancho dado."""

        texto = str(texto or "")

        if ancho <= 0 or not texto:
            return ""

        clave = (texto, ancho, id(fuente))

        guardado = self._cache_texto.get(clave)

        if guardado is not None:
            return guardado

        if fuente.measure(texto) <= ancho:
            resultado = texto

        else:

            bajo, alto = 0, len(texto)

            while bajo < alto:

                medio = (bajo + alto + 1) // 2

                if fuente.measure(texto[:medio] + "…") <= ancho:
                    bajo = medio
                else:
                    alto = medio - 1

            resultado = (texto[:bajo].rstrip() + "…") if bajo else ""

        if len(self._cache_texto) > 4000:
            self._cache_texto.clear()

        self._cache_texto[clave] = resultado

        return resultado

    def _rect_redondeado(self, x1, y1, x2, y2, r, **kw):
        puntos = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]

        return self.canvas.create_polygon(puntos, smooth=True, **kw)

    def _rango(self):
        """Índices de la primera y la última fila que se ven."""

        n = len(self.items)

        if n == 0 or self._alto <= 0:
            return 0, -1

        primera = max(0, int(self.offset // self.alto_fila))
        ultima = min(n - 1, int((self.offset + self._alto) // self.alto_fila))

        return primera, ultima

    def _limitar(self, valor):
        maximo = max(0, len(self.items) * self.alto_fila - self._alto)

        return max(0, min(int(valor), maximo))

    def _fila_en(self, y):
        indice = int((y + self.offset) // self.alto_fila)

        if 0 <= indice < len(self.items):
            return self.items[indice]

        return None

    def _hit_en(self, x, y):
        for x1, y1, x2, y2, nombre, item in self._hits:

            if x1 <= x <= x2 and y1 <= y <= y2:
                return nombre, item

        return None

    # ========================================================
    # API PARA LA APP
    # ========================================================

    # --- selección ---

    def seleccionados(self):
        """Las filas seleccionadas, en el orden de la lista."""

        if not self.seleccion:
            return []

        return [item for item in self.items if id(item) in self.seleccion]

    def _notificar_seleccion(self):

        self.solicitar_redibujo()

        if self.al_cambiar_seleccion:
            try:
                self.al_cambiar_seleccion()
            except Exception as ex:
                print(f"[GUI] Error en la selección: {ex}")

    def seleccionar_todo(self):

        self.seleccion = {id(item) for item in self.items}

        self._ancla = self.items[0] if self.items else None

        self._notificar_seleccion()

    def limpiar_seleccion(self):

        if self.seleccion:
            self.seleccion = set()
            self._notificar_seleccion()

    def todo_seleccionado(self):
        return bool(self.items) and len(self.seleccion) >= len(self.items)

    def _seleccionar_rango(self, item, sumar):

        if self._ancla is None or id(self._ancla) not in self._indice:
            self._ancla = item

        a = self._indice[id(self._ancla)]
        b = self._indice[id(item)]

        if a > b:
            a, b = b, a

        rango = {id(x) for x in self.items[a:b + 1]}

        self.seleccion = (self.seleccion | rango) if sumar else rango

    def _clic_en_fila(self, item, ctrl, shift):

        if shift:
            self._seleccionar_rango(item, sumar=ctrl)

        elif ctrl:

            if id(item) in self.seleccion:
                self.seleccion.discard(id(item))
            else:
                self.seleccion.add(id(item))

            self._ancla = item

        else:

            if self.seleccion == {id(item)}:
                self.seleccion = set()
            else:
                self.seleccion = {id(item)}

            self._ancla = item

        self._notificar_seleccion()

    def set_items(self, items, resetear=False):

        self.items = items

        self._indice = {id(item): i for i, item in enumerate(items)}

        # Solo se conserva la selección de lo que sigue visible.
        if self.seleccion:

            vigentes = {i for i in self.seleccion if i in self._indice}

            if vigentes != self.seleccion:
                self.seleccion = vigentes
                self._notificar_seleccion()

        self._hover_item = None
        self._hover_hit = None

        if resetear:
            self.offset = 0

        self.solicitar_redibujo()

    def ir_al_final(self):
        """Lleva el scroll al final de la lista."""

        self.offset = self._limitar(len(self.items) * self.alto_fila)
        self.solicitar_redibujo()

    def set_mensaje_vacio(self, texto):

        if texto != self.mensaje_vacio:

            self.mensaje_vacio = texto

            if not self.items:
                self.solicitar_redibujo()

    def refrescar_item(self, item):
        """Una fila cambió: se redibuja solo si está a la vista."""

        indice = self._indice.get(id(item))

        if indice is None:
            return

        primera, ultima = self._rango()

        if primera <= indice <= ultima:
            self.solicitar_redibujo()

    def solicitar_redibujo(self):
        """Pide un redibujo; varias peticiones seguidas se juntan en una."""

        if self._redibujo_id is None:

            try:
                self._redibujo_id = self.after_idle(self._redibujar)
            except Exception:
                self._redibujo_id = None

    # ========================================================
    # ANIMACIÓN DE LAS BARRAS DE PROGRESO
    # ========================================================

    def animar(self):

        if self._anim_id is None:

            try:
                self._anim_id = self.after(33, self._tick_animacion)
            except Exception:
                self._anim_id = None

    def _tick_animacion(self):

        self._anim_id = None

        cambio = False

        primera, ultima = self._rango()

        for i in range(primera, ultima + 1):

            item = self.items[i]

            if item.modo != "progreso":
                continue

            diferencia = item.progreso_objetivo - item.progreso_actual

            if abs(diferencia) > 0.001:
                item.progreso_actual += diferencia * 0.25
                cambio = True

            elif diferencia != 0:
                item.progreso_actual = item.progreso_objetivo
                cambio = True

        if cambio:
            self.solicitar_redibujo()
            self.animar()

    # ========================================================
    # DIBUJO
    # ========================================================

    def _redibujar(self):

        self._redibujo_id = None

        try:

            c = self.canvas

            c.delete("all")

            self._hits = []

            self._necesita_anim = False

            ancho, alto_total = self._ancho, self._alto

            if ancho < 10 or alto_total < 10:
                return

            n = len(self.items)

            if n == 0:

                c.create_text(
                    ancho / 2,
                    alto_total / 2 - self.e(10),
                    text=self.mensaje_vacio,
                    fill=C_TEXTO_3,
                    font=self.f_vacio,
                    justify="center",
                )

                self._actualizar_scrollbar()

                return

            self.offset = self._limitar(self.offset)

            primera, ultima = self._rango()

            for i in range(primera, ultima + 1):

                y = i * self.alto_fila - self.offset

                self._dibujar_fila(self.items[i], y)

            self._actualizar_scrollbar()

            if self._necesita_anim:
                self.animar()

        except Exception as ex:
            print(f"[GUI] Error dibujando la lista: {ex}")

    def _actualizar_scrollbar(self):

        total = len(self.items) * self.alto_fila

        if total <= 0 or total <= self._alto:
            self.scroll.set(0.0, 1.0)

        else:
            self.scroll.set(
                self.offset / total,
                min(1.0, (self.offset + self._alto) / total),
            )

    def _foto(self, clave, crear):
        """ImageTk con caché (Tk necesita que se conserve una referencia)."""

        foto = self._fotos.get(clave)

        if foto is None:
            pil = crear()

            if pil is None:
                return None

            foto = ImageTk.PhotoImage(pil)
            self._fotos[clave] = foto

        return foto

    def _imagen_icono(self, nombre, tam, color):
        return self._foto(("i", nombre, tam, color), lambda: pil_icono(nombre, tam, color))

    def _imagen_fondo(self, ancho, alto, radio, color, borde=None):
        return self._foto(
            ("f", int(ancho), int(alto), int(radio), color, borde),
            lambda: pil_fondo(ancho, alto, radio, color, borde),
        )

    def _dibujar_fila(self, item, y):

        c = self.canvas
        e = self.e

        ancho = self._ancho
        alto = self.alto_fila

        y0 = y + e(1)
        y1 = y + alto - e(1)
        ymed = y + alto / 2

        hover = item is self._hover_item

        seleccionada = id(item) in self.seleccion

        # Fondo y barra de color del estado
        if seleccionada:
            self._rect_redondeado(
                e(2), y0, ancho - e(2), y1, e(7),
                fill=C_SELECCION_HOVER if hover else C_SELECCION,
                outline=C_ACENTO,
                width=1,
            )
        else:
            self._rect_redondeado(
                e(2), y0, ancho - e(2), y1, e(7),
                fill=self.FILA_HOVER if hover else self.FILA,
                outline="",
            )

        self._rect_redondeado(
            e(6), y0 + e(7), e(9), y1 - e(7), e(1),
            fill=item.color,
            outline="",
        )

        # ----------------------------------------------------
        # ZONA DERECHA, siempre en las mismas posiciones:
        #   [ ... ] [ bloque 3 ] [ bloque 2 ] [ bloque 1 ] [x]
        # ----------------------------------------------------

        alto_boton = e(24)
        arriba = ymed - alto_boton / 2
        abajo = ymed + alto_boton / 2

        # Cruz para quitar la fila
        lado_x = e(26)
        x2 = ancho - e(10)
        x1 = x2 - lado_x

        sobre_eliminar = self._hover_hit == ("eliminar", item)

        if sobre_eliminar:
            img = self._imagen_fondo(lado_x, alto_boton, e(6), C_ERROR)
            if img is not None:
                c.create_image(x1, arriba, image=img, anchor="nw")
            else:
                self._rect_redondeado(x1, arriba, x2, abajo, e(5), fill=C_ERROR, outline="")

        tam_cruz = e(14)
        img = self._imagen_icono("cruz", tam_cruz, "white" if sobre_eliminar else C_TEXTO_3)

        if img is not None:
            c.create_image((x1 + x2) / 2, ymed, image=img, anchor="center")
        else:
            c.create_text((x1 + x2) / 2, ymed, text="x", font=self.f_boton,
                          fill="white" if sobre_eliminar else C_TEXTO_3)

        self._hits.append((x1, arriba, x2, abajo, "eliminar", item))

        base = x1 - e(10)

        ancho_boton = e(100)
        separacion = e(6)

        def posicion(n):
            """Posición del bloque n (0 = el más cercano a la cruz)."""
            bx2 = base - n * (ancho_boton + separacion)
            return bx2 - ancho_boton, bx2

        def boton(n, nombre, texto, icono, color, color_hover, borde=None):

            bx1, bx2 = posicion(n)

            activo = self._hover_hit == (nombre, item)

            fondo = color_hover if activo else color

            img = self._imagen_fondo(ancho_boton, alto_boton, e(6), fondo, borde)

            if img is not None:
                c.create_image(bx1, arriba, image=img, anchor="nw")
            else:
                self._rect_redondeado(bx1, arriba, bx2, abajo, e(6), fill=fondo, outline="")

            tam_i = e(13)
            hueco = e(6)
            ancho_texto = self.f_boton.measure(texto)

            total = tam_i + hueco + ancho_texto

            xi = (bx1 + bx2) / 2 - total / 2

            img_i = self._imagen_icono(icono, tam_i, "white")

            if img_i is not None:
                c.create_image(xi, ymed, image=img_i, anchor="w")
                xt = xi + tam_i + hueco
            else:
                xt = xi

            c.create_text(xt, ymed, text=texto, font=self.f_boton, fill="white", anchor="w")

            self._hits.append((bx1, arriba, bx2, abajo, nombre, item))

        izquierda = base + e(4)

        if item.modo == "accion":

            boton(0, "opciones", "Opciones", "lista", C_BOTON, C_BOTON_HOVER, C_BORDE)
            boton(1, "reintentar", "Reintentar", "reintentar", C_ACENTO, C_ACENTO_HOVER)
            boton(2, "youtube", "YouTube", "play", C_YT, C_YT_HOVER)

            izquierda = posicion(2)[0]

        elif item.modo == "progreso":

            boton(0, "cancelar", "Cancelar", "cruz", C_ERROR, C_ERROR_HOVER)
            boton(1, "siguiente", "Otro match", "siguiente", C_BOTON, C_BOTON_HOVER, C_BORDE)

            # Barra y porcentaje en el lugar del tercer bloque, más ancha que antes.
            derecha = posicion(1)[0] - e(12)

            c.create_text(
                derecha, ymed,
                text=f"{int(round(item.progreso_actual * 100))}%",
                font=self.f_boton,
                fill=C_TEXTO,
                anchor="e",
            )

            ancho_barra = e(160)
            alto_barra = e(8)

            bx2 = derecha - e(44)
            bx1 = bx2 - ancho_barra

            self._rect_redondeado(
                bx1, ymed - alto_barra / 2, bx2, ymed + alto_barra / 2, alto_barra / 2,
                fill=C_FONDO,
                outline="",
            )

            relleno = max(0.0, min(1.0, item.progreso_actual))

            if relleno > 0:
                fin = bx1 + max(alto_barra, ancho_barra * relleno)
                self._rect_redondeado(
                    bx1, ymed - alto_barra / 2, fin, ymed + alto_barra / 2, alto_barra / 2,
                    fill=C_ACENTO,
                    outline="",
                )

            if abs(item.progreso_objetivo - item.progreso_actual) > 0.001:
                self._necesita_anim = True

            izquierda = bx1

        limite = izquierda - e(12)

        # ----------------------------------------------------
        # TEXTOS (título, artista, estado, detalle)
        # ----------------------------------------------------

        c.create_text(
            self._x_titulo, ymed,
            text=self._ajustar(item.cancion_data.get("nombre", ""), self._col_titulo, self.f_titulo),
            font=self.f_titulo,
            fill=C_TEXTO,
            anchor="w",
        )

        c.create_text(
            self._x_artista, ymed,
            text=self._ajustar(item.cancion_data.get("artista", ""), self._col_artista, self.f_artista),
            font=self.f_artista,
            fill=C_TEXTO_2,
            anchor="w",
        )

        c.create_text(
            self._x_estado, ymed,
            text=self._ajustar(item.texto_estado, self._col_estado, self.f_estado),
            font=self.f_estado,
            fill=item.color,
            anchor="w",
        )

        ancho_detalle = limite - self._x_detalle

        if item.detalle and ancho_detalle > e(30):

            c.create_text(
                self._x_detalle, ymed,
                text=self._ajustar(item.detalle, ancho_detalle, self.f_detalle),
                font=self.f_detalle,
                fill=C_TEXTO_3,
                anchor="w",
            )

    # ========================================================
    # EVENTOS
    # ========================================================

    def _on_configure(self, event):

        self._ancho = event.width
        self._alto = event.height

        self._calcular_columnas()

        self.offset = self._limitar(self.offset)

        self.solicitar_redibujo()

    def _on_motion(self, event):

        hit = self._hit_en(event.x, event.y)

        item = self._fila_en(event.y)

        nuevo_hit = (hit[0], hit[1]) if hit else None

        if item is not self._hover_item or nuevo_hit != self._hover_hit:

            self._hover_item = item
            self._hover_hit = nuevo_hit

            try:
                self.canvas.configure(cursor="hand2" if nuevo_hit else "")
            except Exception:
                pass

            self.solicitar_redibujo()

        self._ocultar_tooltip()
        self._programar_tooltip(event)

    def _on_leave(self, _event=None):

        self._ocultar_tooltip()

        if self._hover_item is not None or self._hover_hit is not None:

            self._hover_item = None
            self._hover_hit = None

            self.solicitar_redibujo()

    def _on_click(self, event):

        self._ocultar_tooltip()

        try:
            self.canvas.focus_set()
        except Exception:
            pass

        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)

        hit = self._hit_en(event.x, event.y)

        # Con Ctrl o Mayús el clic siempre es de selección, incluso sobre un botón.
        if ctrl or shift or not hit:

            item = self._fila_en(event.y)

            if item is None:

                if not (ctrl or shift):
                    self.limpiar_seleccion()

                return

            self._clic_en_fila(item, ctrl, shift)

            return

        nombre, item = hit

        accion = self.acciones.get(nombre)

        if accion:
            accion(item)

    def _on_doble_click(self, event):

        if self._hit_en(event.x, event.y):
            return

        item = self._fila_en(event.y)

        # Doble clic en una canción que requiere atención: abre las opciones.
        if item is not None and item.seccion == "atencion":

            accion = self.acciones.get("opciones")

            if accion:
                accion(item)

    def _on_menu(self, event):
        """Menú del clic derecho."""

        item = self._fila_en(event.y)

        if item is None:
            return

        self._ocultar_tooltip()

        # Clic derecho sobre algo no seleccionado: pasa a ser la selección.
        if id(item) not in self.seleccion:
            self.seleccion = {id(item)}
            self._ancla = item
            self._notificar_seleccion()

        if len(self.seleccion) > 1 and self.menu_lote:
            self.menu_lote(event.x_root, event.y_root)
            return

        menu = menu_tema(self.canvas)

        def agregar(texto, nombre):
            menu.add_command(
                label=texto,
                command=lambda: self.acciones[nombre](item),
            )

        if item.seccion == "atencion":
            agregar("Reintentar", "reintentar")
            agregar("Ver opciones", "opciones")
            agregar("Descargar de YouTube Music (MP3 320)", "youtube")

        elif item.seccion == "proceso":
            agregar("Otro match", "siguiente")
            agregar("Cancelar", "cancelar")

        if menu.index("end") is not None:
            menu.add_separator()

        menu.add_command(
            label="Copiar «artista - título»",
            command=lambda: self._copiar(item),
        )

        agregar("Quitar de la lista", "eliminar")

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copiar(self, item):

        try:
            self.clipboard_clear()
            self.clipboard_append(
                f"{item.cancion_data.get('artista', '')} - {item.cancion_data.get('nombre', '')}"
            )
        except Exception:
            pass

    # ========================================================
    # SCROLL
    # ========================================================

    def _desplazar(self, delta):

        nuevo = self._limitar(self.offset + delta)

        if nuevo != self.offset:

            self.offset = nuevo

            self._ocultar_tooltip()

            self.solicitar_redibujo()

    def _yview(self, *args):
        """Lo llama la barra de scroll al arrastrarla."""

        total = len(self.items) * self.alto_fila

        if not args or total <= 0:
            return

        try:

            if args[0] == "moveto":

                nuevo = self._limitar(float(args[1]) * total)

            elif args[0] == "scroll":

                cantidad = int(float(args[1]))

                paso = self.alto_fila if args[2] == "units" else int(self._alto * 0.9)

                nuevo = self._limitar(self.offset + cantidad * paso)

            else:
                return

        except (TypeError, ValueError, IndexError):
            return

        if nuevo != self.offset:
            self.offset = nuevo
            self.solicitar_redibujo()

    def _on_rueda(self, event):

        try:
            bajo_el_puntero = self.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return

        if bajo_el_puntero is not self.canvas:
            return

        numero = getattr(event, "num", None)

        if numero == 4:
            pasos = -3

        elif numero == 5:
            pasos = 3

        else:

            delta = getattr(event, "delta", 0)

            if delta == 0:
                return

            pasos = -3 if delta > 0 else 3

            # Los mouse con scroll fino mandan varios pasos a la vez.
            if abs(delta) >= 240:
                pasos = pasos * int(abs(delta) // 120)

        self._desplazar(pasos * self.alto_fila)

    # ========================================================
    # TOOLTIP (texto completo al detenerse sobre una fila)
    # ========================================================

    def _programar_tooltip(self, event):

        self._tt_pos = (event.x, event.y)

        try:
            self._tt_id = self.canvas.after(500, self._mostrar_tooltip)
        except Exception:
            self._tt_id = None

    def _mostrar_tooltip(self):

        self._tt_id = None

        try:

            x, y = self._tt_pos

            if self._hit_en(x, y):
                return

            item = self._fila_en(y)

            if item is None:
                return

            texto = ""

            if self._x_titulo <= x < self._x_artista - self.e(4):
                texto = (
                    f"{item.cancion_data.get('nombre', '')}\n"
                    f"{item.cancion_data.get('artista', '')}  •  {item.cancion_data.get('album', '')}"
                )

            elif self._x_artista <= x < self._x_estado - self.e(4):
                texto = item.cancion_data.get("artista", "")

            elif x >= self._x_detalle:
                texto = item.detalle

            if not texto or self._tt_ventana is not None:
                return

            px = self.canvas.winfo_pointerx() + 14
            py = self.canvas.winfo_pointery() + 18

            ventana = tk.Toplevel(self.canvas)
            ventana.wm_overrideredirect(True)
            ventana.wm_geometry(f"+{px}+{py}")

            try:
                ventana.attributes("-topmost", True)
            except Exception:
                pass

            tk.Label(
                ventana,
                text=texto,
                justify="left",
                bg=C_FONDO_PROFUNDO,
                fg=C_TEXTO,
                padx=8,
                pady=5,
                font=("Segoe UI", 9),
                wraplength=520,
                borderwidth=1,
                relief="solid",
            ).pack()

            self._tt_ventana = ventana

        except Exception:
            self._tt_ventana = None

    def _ocultar_tooltip(self):

        if self._tt_id is not None:

            try:
                self.canvas.after_cancel(self._tt_id)
            except Exception:
                pass

            self._tt_id = None

        if self._tt_ventana is not None:

            try:
                self._tt_ventana.destroy()
            except Exception:
                pass

            self._tt_ventana = None


# ============================================================
# APLICACIÓN PRINCIPAL
# ============================================================

class App(ctk.CTk):

    # (clave, texto, ancho del botón)
    FILTROS = (
        ("todas", "Todas", 84),
        ("proceso", "En proceso", 116),
        ("pendientes", "Pendientes", 116),
        ("completadas", "Completadas", 130),
        ("atencion", "Atención", 104),
    )

    def __init__(self):
        super().__init__()

        self.title(NOMBRE_APP)
        self.minsize(1000, 560)
        self.configure(fg_color=C_FONDO)

        poner_icono(self)
        centrar_ventana(self, 1180, 760)

        self.downloader = None
        self.app_lista = False

        self.canciones = []

        # Todas las canciones (ItemCancion), en el orden de la playlist
        self.filas = []

        # Filtro de la lista
        self.filtro = "todas"
        self._palabras_busqueda = []
        self._busqueda_id = None

        # Refresco de la lista (se junta en uno solo aunque lo pidan muchas veces)
        self._refresco_id = None
        self._refresco_resetear = False

        # Cuántas canciones hay en cada grupo
        self.conteo = {"pendientes": 0, "proceso": 0, "completadas": 0, "atencion": 0}

        # Si una canción no está en Soulseek, probar automáticamente YouTube Music
        self.fallback_youtube = False

        # Trabajo en segundo plano
        self.planificador = None
        self._trabajos = 0
        self._resumen_pendiente = False
        self._preparando = False

        # Cola segura: los hilos de descarga dejan aquí sus avisos y la
        # ventana los lee en su hilo principal.
        self._cola_eventos = queue.Queue()

        # Progreso general de inicialización
        self.inicio_progreso_real = 0.0
        self.inicio_progreso_objetivo = 0.0
        self.inicio_animacion_id = None
        self.inicio_actualizacion_pendiente = None
        self.inicio_actualizacion_id = None

        # Control de resize: evita repaints innecesarios durante el arrastre.
        self._resize_id = None

        self.crear_interfaz()

        # La ventana principal no se muestra hasta que la app esté lista:
        # mientras tanto solo se ve la pantalla de inicio.
        self.withdraw()
        self.crear_pantalla_inicio()

        self.bind("<Configure>", self._evento_resize, add="+")
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)

        self.after(60, self._drenar_cola_eventos)
        self.after(150, self.inicializar_backend)

    # ========================================================
    # CREAR INTERFAZ
    # ========================================================

    def crear_interfaz(self):

        self.frame_top = ctk.CTkFrame(
            self, fg_color=C_PANEL, corner_radius=14, border_width=1, border_color=C_BORDE
        )
        self.frame_top.pack(pady=(14, 8), padx=16, fill="x")

        self.frame_title_row = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_title_row.pack(fill="x", padx=16, pady=(12, 4))

        # Marca: logo + nombre
        logo = cargar_logo(48)
        if logo is not None:
            self._logo_cabecera = logo
            ctk.CTkLabel(self.frame_title_row, image=logo, text="").pack(side="left", padx=(0, 10))

        marca = ctk.CTkFrame(self.frame_title_row, fg_color="transparent")
        marca.pack(side="left")

        self.label_titulo = ctk.CTkLabel(
            marca,
            text=NOMBRE_APP,
            font=("Segoe UI", 20, "bold"),
            text_color=C_TEXTO,
            anchor="w",
        )
        self.label_titulo.pack(anchor="w")

        ctk.CTkLabel(
            marca,
            text="Playlists, álbumes y canciones de Spotify en tu carpeta",
            font=("Segoe UI", 10),
            text_color=C_TEXTO_3,
            anchor="w",
        ).pack(anchor="w")

        self.btn_login = ctk.CTkButton(
            self.frame_title_row,
            text="Cuenta de Soulseek",
            image=icono_ctk("usuario", 15, C_TEXTO_2),
            compound="left",
            height=30,
            width=164,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            border_color=C_BORDE,
            hover_color=C_BOTON,
            text_color=C_TEXTO_2,
            font=("Segoe UI", 11),
            command=self.abrir_login,
            state="disabled",
        )
        self.btn_login.pack(side="right")

        self.btn_actualizar = ctk.CTkButton(
            self.frame_title_row,
            text=f"v{actualizador.VERSION}",
            image=icono_ctk("reintentar", 15, C_TEXTO_2),
            compound="left",
            height=30,
            width=88,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            border_color=C_BORDE,
            hover_color=C_BOTON,
            text_color=C_TEXTO_2,
            font=("Segoe UI", 11),
            command=lambda: self.buscar_actualizaciones(manual=True),
        )
        self.btn_actualizar.pack(side="right", padx=(0, 8))

        self.btn_ajustes = ctk.CTkButton(
            self.frame_title_row,
            text="Ajustes",
            image=icono_ctk("engranaje", 15, C_TEXTO_2),
            compound="left",
            height=30,
            width=96,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            border_color=C_BORDE,
            hover_color=C_BOTON,
            text_color=C_TEXTO_2,
            font=("Segoe UI", 11),
            command=self.abrir_ajustes,
            state="disabled",
        )
        self.btn_ajustes.pack(side="right", padx=(0, 8))

        # ====================================================
        # IMPORTAR PLAYLIST
        # ====================================================

        self.frame_search_row = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_search_row.pack(fill="x", padx=16, pady=(8, 14))

        self.entry_url = ctk.CTkEntry(
            self.frame_search_row,
            placeholder_text="Pega un enlace de Spotify: canción, álbum, EP o playlist...",
            height=40,
            corner_radius=10,
            fg_color=C_FONDO,
            border_color=C_BORDE,
            text_color=C_TEXTO,
            font=("Segoe UI", 12),
        )
        self.entry_url.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.btn_importar = ctk.CTkButton(
            self.frame_search_row,
            text="Importar",
            height=40,
            width=120,
            corner_radius=10,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            text_color="white",
            font=("Segoe UI", 13, "bold"),
            command=self.importar_playlist,
            state="disabled",
        )
        self.btn_importar.pack(side="right")

        # ====================================================
        # FILTROS DE LA LISTA
        # ====================================================

        self.frame_filtros = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_filtros.pack(fill="x", padx=16, pady=(0, 4))

        self.chips = {}

        for clave, texto, ancho in self.FILTROS:

            boton = ctk.CTkButton(
                self.frame_filtros,
                text=texto,
                width=ancho,
                height=30,
                corner_radius=15,
                fg_color=C_PANEL,
                hover_color=C_BOTON,
                text_color=C_TEXTO_2,
                font=("Segoe UI", 11),
                command=lambda c=clave: self.cambiar_filtro(c),
            )
            boton.pack(side="left", padx=(0, 6))

            self.chips[clave] = boton

        self.entry_buscar = ctk.CTkEntry(
            self.frame_filtros,
            width=190,
            height=30,
            corner_radius=15,
            placeholder_text="Buscar en la lista...",
            fg_color=C_FONDO,
            border_color=C_BORDE,
            text_color=C_TEXTO,
            font=("Segoe UI", 11),
        )
        self.entry_buscar.pack(side="left", padx=(8, 0))
        self.entry_buscar.bind("<KeyRelease>", lambda _e: self._programar_busqueda())

        # Acciones que aparecen según haya canciones en cada grupo
        self.btn_reintentar_todas = ctk.CTkButton(
            self.frame_filtros,
            text="Reintentar todas",
            image=icono_ctk("reintentar", 14, "white"),
            compound="left",
            width=150,
            height=30,
            corner_radius=15,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            text_color="white",
            font=("Segoe UI", 11, "bold"),
            command=self.reintentar_todas,
        )

        self.btn_limpiar = ctk.CTkButton(
            self.frame_filtros,
            text="Limpiar",
            image=icono_ctk("papelera", 14, C_TEXTO),
            compound="left",
            width=100,
            height=30,
            corner_radius=15,
            fg_color=C_BOTON,
            hover_color=C_BOTON_HOVER,
            text_color="white",
            font=("Segoe UI", 11),
            command=self.limpiar_completadas,
        )

        # ====================================================
        # SELECCIÓN MÚLTIPLE, PAUSA Y CANCELAR TODO
        # ====================================================

        self._firma_barra = None
        self._firma_pausa = None
        self._solo_lote = None
        self.pausado = False

        self.frame_acciones = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_acciones.pack(fill="x", padx=16, pady=(2, 2))

        self.btn_cancelar_todo = ctk.CTkButton(
            self.frame_acciones,
            text="Cancelar todo",
            image=icono_ctk("cruz", 14, C_TEXTO),
            compound="left",
            width=124,
            height=30,
            corner_radius=15,
            fg_color=C_BOTON,
            hover_color=C_ERROR,
            text_color=C_TEXTO,
            font=("Segoe UI", 11),
            command=self.cancelar_todo,
            state="disabled",
        )
        self.btn_cancelar_todo.pack(side="right")

        self.btn_pausar = ctk.CTkButton(
            self.frame_acciones,
            text="Pausar",
            image=icono_ctk("pausa", 14, C_TEXTO),
            compound="left",
            width=104,
            height=30,
            corner_radius=15,
            fg_color=C_BOTON,
            hover_color=C_BOTON_HOVER,
            text_color=C_TEXTO,
            font=("Segoe UI", 11),
            command=self.pausar_o_reanudar,
            state="disabled",
        )
        self.btn_pausar.pack(side="right", padx=(0, 6))

        self.btn_sel_todas = ctk.CTkButton(
            self.frame_acciones,
            text="Seleccionar todas",
            image=icono_ctk("casilla", 14, C_TEXTO_2),
            compound="left",
            width=152,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            border_width=1,
            border_color=C_BORDE,
            hover_color=C_BOTON,
            text_color=C_TEXTO_2,
            font=("Segoe UI", 11),
            command=self.alternar_seleccion_total,
        )
        self.btn_sel_todas.pack(side="left")

        self.lbl_seleccion = ctk.CTkLabel(
            self.frame_acciones,
            text="",
            font=("Segoe UI", 11, "bold"),
            text_color=C_ACENTO,
        )

        self.frame_lote = ctk.CTkFrame(self.frame_acciones, fg_color="transparent")

        def boton_lote(texto, icono, color, hover, comando, ancho):
            return ctk.CTkButton(
                self.frame_lote,
                text=texto,
                image=icono_ctk(icono, 14, "white"),
                compound="left",
                width=ancho,
                height=30,
                corner_radius=15,
                fg_color=color,
                hover_color=hover,
                text_color="white",
                font=("Segoe UI", 11, "bold"),
                command=comando,
            )

        self.btn_lote_descargar = boton_lote("Descargar", "descargar", C_CTA, C_CTA_HOVER, self.lote_descargar, 112)
        self.btn_lote_reintentar = boton_lote("Reintentar", "reintentar", C_ACENTO, C_ACENTO_HOVER, self.lote_reintentar, 112)
        self.btn_lote_youtube = boton_lote("YouTube", "play", C_YT, C_YT_HOVER, self.lote_youtube, 104)
        self.btn_lote_cancelar = boton_lote("Cancelar", "cruz", C_ERROR, C_ERROR_HOVER, self.lote_cancelar, 104)
        self.btn_lote_quitar = boton_lote("Quitar", "papelera", C_BOTON, C_BOTON_HOVER, self.lote_quitar, 92)

        self.btn_lote_soltar = ctk.CTkButton(
            self.frame_lote,
            text="",
            image=icono_ctk("cruz", 14, C_TEXTO_2),
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            border_width=1,
            border_color=C_BORDE,
            hover_color=C_BOTON,
            command=lambda: self.lista.limpiar_seleccion(),
        )

        self._botones_lote = (
            self.btn_lote_descargar,
            self.btn_lote_reintentar,
            self.btn_lote_youtube,
            self.btn_lote_cancelar,
            self.btn_lote_quitar,
            self.btn_lote_soltar,
        )

        # ====================================================
        # LISTA (virtual: solo dibuja las filas que se ven)
        # ====================================================

        self.frame_lista_container = ctk.CTkFrame(
            self, fg_color=C_FONDO, corner_radius=12, border_width=1, border_color=C_BORDE
        )
        self.frame_lista_container.pack(pady=6, padx=16, fill="both", expand=True)

        self.lista = ListaVirtual(
            self.frame_lista_container,
            acciones={
                "reintentar": self.reintentar_item,
                "opciones": self.abrir_opciones,
                "cancelar": self.cancelar_item,
                "siguiente": self.siguiente_match_item,
                "eliminar": self.eliminar_item,
                "youtube": self.descargar_youtube_item,
            },
            mensaje_vacio=(
                "Todavía no hay canciones\n"
                "Pega un enlace de Spotify arriba y pulsa Importar"
            ),
        )
        self.lista.pack(fill="both", expand=True, padx=4, pady=4)

        self.lista.al_cambiar_seleccion = self._actualizar_barra_seleccion
        self.lista.menu_lote = self._menu_lote

        self.bind("<Control-a>", self._atajo_seleccionar_todo)
        self.bind("<Control-A>", self._atajo_seleccionar_todo)
        self.bind("<Escape>", self._atajo_escape)

        # ====================================================
        # BARRA INFERIOR
        # ====================================================

        self.frame_bottom = ctk.CTkFrame(
            self, fg_color=C_PANEL, corner_radius=14, border_width=1, border_color=C_BORDE
        )
        self.frame_bottom.pack(pady=(8, 14), padx=16, fill="x")

        self.btn_descargar = ctk.CTkButton(
            self.frame_bottom,
            text=TEXTO_BOTON_DESCARGAR,
            image=icono_ctk("descargar", 18, "white"),
            compound="left",
            height=40,
            corner_radius=10,
            fg_color=C_CTA,
            hover_color=C_CTA_HOVER,
            text_color="white",
            font=("Segoe UI", 13, "bold"),
            command=self.iniciar_descargas,
            state="disabled",
        )
        self.btn_descargar.pack(side="left", padx=12, pady=8)

        self.btn_explorador = ctk.CTkButton(
            self.frame_bottom,
            text="Abrir carpeta",
            image=icono_ctk("carpeta", 16, C_TEXTO_2),
            compound="left",
            height=40,
            width=150,
            corner_radius=10,
            fg_color="transparent",
            border_width=1,
            border_color=C_BORDE,
            hover_color=C_BOTON,
            text_color=C_TEXTO_2,
            font=("Segoe UI", 12),
            command=self.abrir_explorador,
            state="disabled",
        )
        self.btn_explorador.pack(side="right", padx=12, pady=8)

        self.var_fallback_youtube = tk.BooleanVar(value=False)

        self.chk_youtube = ctk.CTkCheckBox(
            self.frame_bottom,
            text="Si no está en Soulseek,\nprobar YouTube Music",
            variable=self.var_fallback_youtube,
            command=self._al_cambiar_fallback,
            font=("Segoe UI", 11),
            text_color=C_TEXTO_2,
            fg_color=C_ACENTO,
            hover_color=C_ACENTO_HOVER,
            border_color=C_BORDE,
            checkbox_width=18,
            checkbox_height=18,
        )
        self.chk_youtube.pack(side="right", padx=(0, 8), pady=8)

        self.frame_global = ctk.CTkFrame(self.frame_bottom, fg_color="transparent")
        self.frame_global.pack(side="left", fill="x", expand=True, padx=8)

        self.lbl_global = ctk.CTkLabel(
            self.frame_global,
            text="",
            font=("Segoe UI", 11),
            text_color=C_TEXTO_2,
            anchor="w",
        )
        self.lbl_global.pack(fill="x")

        self.progress_global = ctk.CTkProgressBar(
            self.frame_global,
            height=8,
            corner_radius=4,
            fg_color=C_FONDO,
            progress_color=C_TEAL,
        )
        self.progress_global.set(0)
        self.progress_global.pack(fill="x", pady=(2, 0))

        self._actualizar_resumen()

    def _evento_resize(self, _event=None):
        """Marca un resize sin lanzar trabajo de UI por cada evento."""
        if self._resize_id is not None:
            try:
                self.after_cancel(self._resize_id)
            except Exception:
                pass

        self._resize_id = self.after(120, self._fin_resize)

    def _fin_resize(self):
        self._resize_id = None

    # ========================================================
    # PANTALLA DE INICIO
    # ========================================================

    def crear_pantalla_inicio(self):

        self.ventana_inicio = VentanaInicio(self)

        # La app usa estos nombres para ir informando el avance.
        self.frame_inicio = self.ventana_inicio.marco
        self.lbl_inicio_estado = self.ventana_inicio.lbl_estado
        self.lbl_inicio_detalle = self.ventana_inicio.lbl_detalle
        self.progress_inicio = self.ventana_inicio.progress

    def actualizar_progreso_inicio(self, progreso, estado=None, detalle=None):

        try:
            valor = max(0.0, min(1.0, float(progreso)))
        except (TypeError, ValueError):
            valor = 0.0

        # Solo conservamos el último estado. Si el backend manda varios
        # avances seguidos, Tkinter no necesita pintar cada uno.
        self.inicio_actualizacion_pendiente = (valor, estado, detalle)

        if self.inicio_actualizacion_id is None:
            self.inicio_actualizacion_id = self.after(30, self._procesar_progreso_inicio)

    def _procesar_progreso_inicio(self):
        self.inicio_actualizacion_id = None

        try:
            if not self.frame_inicio.winfo_exists():
                return

            pendiente = self.inicio_actualizacion_pendiente
            self.inicio_actualizacion_pendiente = None
            if pendiente is None:
                return

            valor, estado, detalle = pendiente
            self.inicio_progreso_real = valor
            self.inicio_progreso_objetivo = valor

            if estado is not None:
                self.lbl_inicio_estado.configure(text=estado)
            if detalle is not None:
                self.lbl_inicio_detalle.configure(text=detalle)

            self._iniciar_animacion_inicio()

        except Exception as e:
            print(f"[GUI] Error progreso inicial: {e}")

    def _iniciar_animacion_inicio(self):
        if self.inicio_animacion_id is None:
            self._animar_progreso_inicio()

    def _animar_progreso_inicio(self):
        self.inicio_animacion_id = None

        try:
            if not self.frame_inicio.winfo_exists():
                return

            actual = self.progress_inicio.get()
            objetivo = self.inicio_progreso_objetivo
            distancia = objetivo - actual

            if abs(distancia) < 0.001:
                actual = objetivo
            else:
                # Misma sensación suave de las barras de descarga.
                actual += distancia * 0.20

            self.progress_inicio.set(actual)

            if abs(objetivo - actual) >= 0.001:
                self.inicio_animacion_id = self.after(16, self._animar_progreso_inicio)

        except Exception:
            pass

    # ========================================================
    # INICIALIZAR BACKEND
    # ========================================================

    def inicializar_backend(self):

        def tarea():
            try:
                self.actualizar_progreso_inicio(
                    0.05, "Iniciando aplicación...", "Preparando componentes..."
                )
                time.sleep(0.15)

                self.actualizar_progreso_inicio(
                    0.15, "Preparando Soulseek...", "Cargando configuración y credenciales..."
                )

                print("[GUI] Inicializando SoulseekDownloader...")
                self.downloader = SoulseekDownloader()
                print("[GUI] SoulseekDownloader inicializado.")

                # Primera vez (o sin credenciales): pedir la cuenta de Soulseek.
                if not self.downloader.soulseek_configurado():

                    self.actualizar_progreso_inicio(
                        0.25, "Configuración inicial", "Falta configurar tu cuenta de Soulseek..."
                    )

                    evento = threading.Event()
                    resultado_cfg = {"ok": False}

                    def al_terminar(ok):
                        resultado_cfg["ok"] = ok

                        # Vuelve la pantalla de inicio mientras se termina de arrancar.
                        if ok:
                            try:
                                self.ventana_inicio.deiconify()
                            except Exception:
                                pass

                        evento.set()

                    def abrir_config():
                        # Se oculta la pantalla de inicio: la ventana de la cuenta
                        # de Soulseek es lo único que se ve.
                        try:
                            self.ventana_inicio.withdraw()
                        except Exception:
                            pass

                        VentanaConfigSoulseek(
                            self, self.downloader, on_fin=al_terminar, primera_vez=True
                        )

                    self._llamar_en_ventana(abrir_config)

                    evento.wait()

                    if not resultado_cfg["ok"]:
                        self._llamar_en_ventana(self.configuracion_pendiente)
                        return

                self.actualizar_progreso_inicio(
                    0.35, "Configurando Soulseek...", "Preparando servidor local..."
                )
                time.sleep(0.15)

                self.actualizar_progreso_inicio(
                    0.50,
                    "Iniciando servidor slskd...",
                    "Puede tardar hasta un minuto si es la primera vez...",
                )

                print("[GUI] Iniciando servidor slskd...")
                servidor_ok = self.downloader.iniciar_servidor_slskd()
                print(f"[GUI] Resultado slskd: {servidor_ok}")

                if not servidor_ok:

                    # slskd funciona pero pide usuario y contraseña: se los pedimos.
                    if getattr(self.downloader, "requiere_login", False):
                        self._llamar_en_ventana(self.pedir_login_inicial)
                    else:
                        self._llamar_en_ventana(self.inicializacion_fallida)

                    return

                self.actualizar_progreso_inicio(
                    0.70, "Servidor iniciado", "Verificando conexión con Soulseek..."
                )
                time.sleep(0.25)

                conectado = False

                try:
                    conectado = self.downloader.verificar_estado_red()
                except Exception as e:
                    print(f"[GUI] Error verificando red: {e}")

                if conectado:
                    self.actualizar_progreso_inicio(
                        0.90, "Conexión establecida", "Finalizando configuración..."
                    )
                else:
                    self.actualizar_progreso_inicio(
                        0.85, "Servidor listo", "Soulseek todavía está conectándose..."
                    )

                time.sleep(0.35)

                self.actualizar_progreso_inicio(1.0, "Aplicación lista", "Todo está preparado.")

                print("[GUI] Aplicación lista.")

                self._llamar_en_ventana(self.finalizar_inicializacion)

            except Exception as e:
                print(f"[GUI] Error durante inicialización: {e}")
                self._llamar_en_ventana(lambda err=e: self.inicializacion_fallida(err))

        threading.Thread(target=tarea, daemon=True).start()

    def finalizar_inicializacion(self):

        self.app_lista = True

        self.btn_login.configure(state="normal")
        self.btn_ajustes.configure(state="normal")
        self.btn_importar.configure(state="normal")
        self.btn_descargar.configure(state="normal")
        self.btn_explorador.configure(state="normal")

        self.after(400, self.ocultar_pantalla_inicio)

        if self._ajuste("youtube_respaldo") and not self.var_fallback_youtube.get():
            self.var_fallback_youtube.set(True)
            self.fallback_youtube = True

        if not getattr(self, "_actualizaciones_comprobadas", False):
            self._actualizaciones_comprobadas = True

            if self._ajuste("buscar_actualizaciones"):
                self.after(2500, lambda: self.buscar_actualizaciones(manual=False))

    def inicializacion_fallida(self, error=None):

        self.app_lista = False

        if error:
            texto = (
                "No se pudo iniciar correctamente la aplicación.\n\n"
                f"Detalle:\n{error}"
            )
        else:
            texto = (
                "No se pudo iniciar el servidor slskd.\n\n"
                "Mira la consola: ahí aparece el motivo (por ejemplo, otro slskd.exe "
                "abierto desde una sesión anterior). Lo que escribió slskd al arrancar "
                "queda también en el archivo slskd_arranque.log."
            )

        print(f"[GUI] {texto}")

        self.ventana_inicio.mostrar_error(
            "No se pudo iniciar",
            "Revisa el archivo slskd_arranque.log (junto al programa) para ver el motivo."
            + (f"\n\n{error}" if error else ""),
        )

    def pedir_login_inicial(self):
        """slskd está funcionando pero exige usuario y contraseña (HTTP 401)."""

        # Se habilita la app y se oculta la pantalla de inicio.
        self.finalizar_inicializacion()

        messagebox.showinfo(
            "slskd pide usuario y contraseña",
            "slskd está funcionando, pero su API exige usuario y contraseña "
            "y no pude averiguarlos automáticamente.\n\n"
            "Escríbelos en la ventana que sigue (los de la web de slskd; "
            "por defecto: slskd / slskd).",
            parent=self,
        )

        VentanaLogin(self, self.downloader)

    def ocultar_pantalla_inicio(self):
        """Cierra la pantalla de inicio y muestra la ventana principal."""

        try:
            if self.ventana_inicio.winfo_exists():
                self.ventana_inicio.destroy()
        except Exception:
            pass

        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    # ========================================================
    # LOGIN / EXPLORADOR
    # ========================================================

    def configuracion_pendiente(self):
        """El usuario cerró la ventana de Soulseek sin configurar nada."""

        self.app_lista = False

        try:
            self.ventana_inicio.deiconify()
        except Exception:
            pass

        self.ventana_inicio.mostrar_error(
            "Falta configurar Soulseek",
            "Sin una cuenta de Soulseek la app no puede buscar ni descargar música.\n"
            "Cierra y vuelve a abrir la aplicación para configurarla.",
        )

    def abrir_login(self):

        if not self.app_lista or self.downloader is None:
            return

        VentanaConfigSoulseek(self, self.downloader)

    # ========================================================
    # ACTUALIZACIONES
    # ========================================================

    def _carpeta_programa(self):
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def buscar_actualizaciones(self, manual=False):
        """Consulta si hay una versión nueva (en segundo plano)."""

        if not actualizador.URL_VERSION.strip():
            if manual:
                messagebox.showinfo(
                    "Actualizaciones",
                    "Las actualizaciones no están configuradas todavía "
                    "(falta URL_VERSION en actualizador.py).",
                    parent=self,
                )
            return

        def tarea():
            try:
                info = actualizador.buscar_actualizacion()
            except Exception as e:
                print(f"[ACTUALIZADOR] No se pudo comprobar: {e}")
                if manual:
                    self._llamar_en_ventana(
                        lambda: messagebox.showwarning(
                            "Actualizaciones",
                            f"No se pudo comprobar si hay actualizaciones.\n\n{e}",
                            parent=self,
                        )
                    )
                return

            if info is None:
                if manual:
                    self._llamar_en_ventana(
                        lambda: messagebox.showinfo(
                            "Actualizaciones",
                            f"Ya tienes la última versión (v{actualizador.VERSION}).",
                            parent=self,
                        )
                    )
                return

            self._llamar_en_ventana(lambda: self._ofrecer_actualizacion(info))

        threading.Thread(target=tarea, daemon=True).start()

    def _ofrecer_actualizacion(self, info):

        texto = f"Hay una versión nueva: v{info['version']} (tienes la v{actualizador.VERSION})."

        if info.get("notas"):
            texto += f"\n\nNovedades:\n{info['notas']}"

        texto += (
            "\n\n¿Actualizar ahora? La app se reiniciará. "
            "Las descargas que estén en curso se interrumpirán."
        )

        if not messagebox.askyesno("Actualización disponible", texto, parent=self):
            return

        self.btn_actualizar.configure(state="disabled", text="Actualizando...")

        def tarea():
            try:
                actualizador.instalar_actualizacion(
                    info,
                    self._carpeta_programa(),
                    progreso=lambda t: self._llamar_en_ventana(
                        lambda t=t: self.btn_actualizar.configure(text=t)
                    ),
                )
            except Exception as e:
                print(f"[ACTUALIZADOR] Error: {e}")

                def fallo(e=e):
                    self.btn_actualizar.configure(
                        state="normal", text=f"⟳ Actualizaciones (v{actualizador.VERSION})"
                    )
                    messagebox.showerror(
                        "Actualizaciones",
                        f"No se pudo actualizar. No se cambió nada.\n\n{e}",
                        parent=self,
                    )

                self._llamar_en_ventana(fallo)
                return

            self._llamar_en_ventana(self._reiniciar_app)

        threading.Thread(target=tarea, daemon=True).start()

    # ========================================================
    # CIERRE
    # ========================================================

    def _hay_actividad(self):
        return self._trabajos > 0 or self.conteo.get("proceso", 0) > 0

    def _al_cerrar(self):
        """Pide confirmación si hay descargas y cierra todo de forma ordenada."""

        if getattr(self, "_cerrando", False):
            return

        if self._hay_actividad() and self._ajuste("confirmar_cierre"):

            if not messagebox.askyesno(
                f"Cerrar {NOMBRE_APP}",
                "Hay descargas en curso.\n\n"
                "Si cierras ahora se detendrán y se eliminarán los archivos incompletos.\n\n"
                "¿Quieres cerrar de todos modos?",
                icon="warning",
                default="no",
                parent=self,
            ):
                return

        self._cierre_limpio()

    def _cierre_limpio(self, reiniciar=False):
        """Cancela todo, cierra slskd, borra lo incompleto y sale (o reinicia la app)."""

        self._cerrando = True

        # La ventana se oculta enseguida; la limpieza sigue por detrás.
        try:
            self.withdraw()
        except Exception:
            pass

        for fila in list(self.filas):
            try:
                fila.token += 1
                fila.control.cancelar.set()
            except Exception:
                pass

        if self.planificador is not None:
            self.planificador.vaciar()

        def tarea():
            try:
                if self.downloader is not None:
                    self.downloader.cerrar_todo()
            except Exception as e:
                print(f"[GUI] Error al cerrar: {e}")

            if reiniciar:
                try:
                    if getattr(sys, "frozen", False):
                        comando = [sys.executable]
                    else:
                        comando = [sys.executable] + sys.argv

                    subprocess.Popen(comando, cwd=str(self._carpeta_programa()))
                except Exception as e:
                    print(f"[GUI] No se pudo reiniciar: {e}")

            os._exit(0)

        threading.Thread(target=tarea, daemon=True).start()

        # Si algo se cuelga, se sale igual a los 12 segundos.
        threading.Timer(12.0, lambda: os._exit(0)).start()

    def _reiniciar_app(self):
        self._cierre_limpio(reiniciar=True)

    def _ajuste(self, clave):
        """Valor de un ajuste (o el predeterminado si todavía no hay downloader)."""

        if self.downloader is not None:
            return self.downloader.ajustes.get(clave)

        return AJUSTES_PREDETERMINADOS.get(clave)

    def abrir_ajustes(self, valores=None):

        if not self.app_lista or self.downloader is None:
            return

        try:
            ventana = getattr(self, "_ventana_ajustes", None)

            if ventana is not None and ventana.winfo_exists():
                ventana.deiconify()
                ventana.lift()
                ventana.focus_force()
                return

        except Exception:
            pass

        self._ventana_ajustes = VentanaAjustes(self, valores=valores)

    def guardar_ajustes(self, nuevos, ventana=None):
        """Guarda los ajustes y los pone en marcha sin reiniciar."""

        anterior = dict(self.downloader.ajustes.datos)

        if not self.downloader.ajustes.actualizar(nuevos):
            messagebox.showwarning(
                "No se pudieron guardar",
                "No pude escribir el archivo de ajustes. Los cambios valen solo hasta cerrar la app.",
                parent=ventana or self,
            )

        self.downloader.aplicar_ajustes()

        datos = self.downloader.ajustes.datos

        # Si la carpeta elegida no sirvió, se avisa.
        elegida = datos["carpeta_descargas"]

        if elegida and os.path.normcase(os.path.normpath(str(self.downloader.download_dir))) != os.path.normcase(
            os.path.normpath(elegida)
        ):
            messagebox.showwarning(
                "Carpeta no disponible",
                f"No pude usar la carpeta «{elegida}». Se usa la carpeta predeterminada.",
                parent=ventana or self,
            )

        # Cuántas canciones se buscan a la vez
        if self.planificador is not None:
            simultaneas = int(datos["max_simultaneas"])
            self.planificador.configurar(simultaneas, max(MAX_EN_PROCESO, simultaneas * 2))

        # Respaldo de YouTube: la casilla sigue al ajuste si cambió
        if bool(datos["youtube_respaldo"]) != bool(anterior["youtube_respaldo"]):
            self.var_fallback_youtube.set(bool(datos["youtube_respaldo"]))
            self._al_cambiar_fallback()

        if ventana is not None:
            try:
                ventana.destroy()
            except Exception:
                pass

    def abrir_explorador(self):

        if not self.app_lista or self.downloader is None:
            return

        self.downloader.abrir_en_explorador()

    # ========================================================
    # IMPORTAR PLAYLIST
    # ========================================================

    def importar_playlist(self):
        """Importa un enlace de Spotify: canción, álbum/EP o playlist."""

        if not self.app_lista:
            return

        texto = self.entry_url.get().strip()

        if not texto:
            messagebox.showwarning(
                "Atención",
                "Pega el enlace de Spotify de una canción, un álbum, un EP o una playlist.",
            )
            return

        analizado = analizar_enlace_spotify(texto)

        if analizado is None:
            messagebox.showerror(
                "Enlace no válido",
                "Ese enlace no parece de Spotify.\n\n"
                "Pega un enlace de una canción, un álbum, un EP o una playlist, por ejemplo:\n"
                "https://open.spotify.com/album/...",
            )
            return

        tipo, identificador = analizado

        self.btn_importar.configure(state="disabled", text="Cargando...")

        def tarea():
            try:
                titulo, pistas = leer_spotify(tipo, identificador)

                self._llamar_en_ventana(
                    lambda: self._importacion_lista(tipo, titulo, pistas)
                )

            except Exception as e:
                self._llamar_en_ventana(
                    lambda err=e: messagebox.showerror(
                        "Error", f"No se pudo leer el enlace de Spotify:\n{err}"
                    )
                )

            finally:
                self._llamar_en_ventana(self.reset_btn_importar)

        threading.Thread(target=tarea, daemon=True).start()

    def _importacion_lista(self, tipo, titulo, pistas):
        """Decide si la importación reemplaza la lista o se agrega al final."""

        # Una canción suelta siempre se agrega: así se puede armar una lista de a una.
        if tipo == "track":
            self._agregar_canciones_ui(pistas)
            self.entry_url.delete(0, "end")
            return

        sin_terminar = [c for c in self.filas if c.seccion != "completadas"]

        if not sin_terminar:
            self._cargar_playlist_ui(pistas)
            self.entry_url.delete(0, "end")
            return

        respuesta = messagebox.askyesnocancel(
            "Importar " + NOMBRES_TIPO.get(tipo, "lista"),
            f"«{titulo or NOMBRES_TIPO.get(tipo, 'Lista')}»: {len(pistas)} canciones.\n\n"
            f"Ya tienes {len(self.filas)} canciones en la lista.\n\n"
            "Sí: reemplazar la lista actual\n"
            "No: agregar las canciones al final\n"
            "Cancelar: no importar",
        )

        if respuesta is None:
            return

        if respuesta:
            self._cargar_playlist_ui(pistas)
        else:
            self._agregar_canciones_ui(pistas)

        self.entry_url.delete(0, "end")

    def _agregar_canciones_ui(self, pistas):
        """Agrega canciones al final de la lista (sin repetir las que ya están)."""

        existentes = {
            (c["artista"].strip().lower(), c["nombre"].strip().lower()) for c in self.canciones
        }

        siguiente = max([c["id"] for c in self.canciones] + [0]) + 1

        nuevas = []

        for p in pistas:

            clave = (p["artista"].strip().lower(), p["nombre"].strip().lower())

            if clave in existentes:
                continue

            existentes.add(clave)

            p = dict(p)
            p["id"] = siguiente
            siguiente += 1

            nuevas.append(p)

        if not nuevas:
            messagebox.showinfo("Importar", "Esas canciones ya están en la lista.")
            return

        for p in nuevas:
            self.canciones.append(p)
            self._nueva_tarjeta(p)

        self._solicitar_refresco(resetear=False)

        # Se muestra el final de la lista para ver lo que se agregó.
        try:
            self.lista.ir_al_final()
        except Exception:
            pass

    @staticmethod
    def _numero(valor):
        try:
            return int(float(valor or 0))
        except (TypeError, ValueError):
            return 0

    def reset_btn_importar(self):

        self.btn_importar.configure(state="normal", text="Importar")

    # ========================================================
    # FILTROS Y LISTA
    # ========================================================

    def cambiar_filtro(self, clave):

        self.filtro = clave

        self._solicitar_refresco(resetear=True)

    def _programar_busqueda(self):
        """Espera un momento tras la última tecla antes de filtrar."""

        if self._busqueda_id is not None:
            try:
                self.after_cancel(self._busqueda_id)
            except Exception:
                pass

        self._busqueda_id = self.after(200, self._aplicar_busqueda)

    def _aplicar_busqueda(self):

        self._busqueda_id = None

        self._palabras_busqueda = self.entry_buscar.get().strip().lower().split()

        self._solicitar_refresco(resetear=True)

    def _coincide(self, card):
        """¿Esta fila debe mostrarse con el filtro y el buscador actuales?"""

        if self.filtro != "todas" and card.seccion != self.filtro:
            return False

        for palabra in self._palabras_busqueda:

            if palabra not in card.texto_busqueda:
                return False

        return True

    def _solicitar_refresco(self, resetear=False):
        """Pide actualizar la lista y los contadores; muchos pedidos seguidos se juntan en uno."""

        if resetear:
            self._refresco_resetear = True

        if self._refresco_id is None:
            self._refresco_id = self.after_idle(self._refrescar_lista)

    def _refrescar_lista(self):

        self._refresco_id = None

        resetear = self._refresco_resetear

        self._refresco_resetear = False

        visibles = [c for c in self.filas if self._coincide(c)]

        self.lista.set_items(visibles, resetear=resetear)

        if self.filas:
            self.lista.set_mensaje_vacio("Ninguna canción coincide con este filtro")
        else:
            self.lista.set_mensaje_vacio(
                "Todavía no hay canciones\nPega un enlace de Spotify arriba y pulsa Importar"
            )

        self._actualizar_resumen()

        self._actualizar_barra_seleccion()

    def _mover(self, card, clave):
        """Cambia el grupo de una canción (pendiente, en proceso, completada, atención)."""

        if card.seccion == clave:
            return

        if card.seccion is not None:
            self.conteo[card.seccion] = max(0, self.conteo[card.seccion] - 1)

        card.seccion = clave

        self.conteo[clave] += 1

        self._solicitar_refresco()

    def _actualizar_resumen(self):
        """Actualiza los botones de filtro, el progreso general y las acciones."""

        total = len(self.filas)

        n = self.conteo

        cantidades = {
            "todas": total,
            "proceso": n["proceso"],
            "pendientes": n["pendientes"],
            "completadas": n["completadas"],
            "atencion": n["atencion"],
        }

        for clave, texto, _ancho in self.FILTROS:

            seleccionado = clave == self.filtro

            cantidad = cantidades[clave]

            if seleccionado:
                fondo = C_ERROR_HOVER if clave == "atencion" else C_ACENTO
                color_texto = "white"

            else:
                fondo = C_PANEL
                color_texto = C_ERROR if (clave == "atencion" and cantidad) else C_TEXTO_2

            self.chips[clave].configure(
                text=f"{texto}  {cantidad}",
                fg_color=fondo,
                text_color=color_texto,
            )

        # Acciones contextuales
        self.btn_reintentar_todas.pack_forget()
        self.btn_limpiar.pack_forget()

        if n["atencion"]:
            self.btn_reintentar_todas.pack(side="right", padx=(6, 0))

        if n["completadas"]:
            self.btn_limpiar.pack(side="right", padx=(6, 0))

        # Pausar / cancelar todo
        activo = self._trabajos > 0

        firma = (activo, self.pausado)

        if firma != self._firma_pausa:

            self._firma_pausa = firma

            if self.pausado:
                self.btn_pausar.configure(
                    text="Reanudar",
                    image=icono_ctk("play", 14, "white"),
                    fg_color=C_ACENTO,
                    hover_color=C_ACENTO_HOVER,
                    text_color="white",
                    state="normal",
                )
            else:
                self.btn_pausar.configure(
                    text="Pausar",
                    image=icono_ctk("pausa", 14, C_TEXTO),
                    fg_color=C_BOTON,
                    hover_color=C_BOTON_HOVER,
                    text_color=C_TEXTO,
                    state="normal" if activo else "disabled",
                )

            self.btn_cancelar_todo.configure(state="normal" if activo else "disabled")

        # Progreso general
        if total:
            hechas = n["completadas"]
            self.progress_global.set(hechas / total)

            if self.pausado:
                self.lbl_global.configure(
                    text=f"En pausa: no se inician canciones nuevas  ·  {hechas} de {total} completadas"
                )
            else:
                self.lbl_global.configure(text=f"{hechas} de {total} completadas")
        else:
            self.progress_global.set(0)
            self.lbl_global.configure(text="")

    # ========================================================
    # LISTA
    # ========================================================

    def _cargar_playlist_ui(self, canciones):
        """Crea todas las filas de golpe: ya no son widgets, así que es instantáneo."""

        self.limpiar_lista()

        self.canciones.clear()
        self.canciones.extend(canciones)

        # Una playlist nueva empieza viendo todas las canciones.
        self.filtro = "todas"
        self.entry_buscar.delete(0, "end")
        self._palabras_busqueda = []

        # Vuelve a mostrar el texto de ayuda del buscador (customtkinter lo oculta al borrar).
        try:
            self.entry_buscar._activate_placeholder()
        except Exception:
            pass

        for cancion in canciones:
            self._nueva_tarjeta(cancion)

        self._solicitar_refresco(resetear=True)

    def _nueva_tarjeta(self, cancion):

        item = ItemCancion(cancion, self.lista)

        self.filas.append(item)

        self._mover(item, "pendientes")

        return item

    def limpiar_lista(self):

        for item in list(self.filas):
            item.control.cancelar.set()
            item.token += 1

        self.filas.clear()

        for clave in self.conteo:
            self.conteo[clave] = 0

        self._solicitar_refresco(resetear=True)

    def eliminar_item(self, widget):

        if widget in self.filas:

            # Si se estaba descargando, se cancela también en slskd
            widget.control.cancelar.set()
            widget.token += 1

            if widget.seccion is not None:
                self.conteo[widget.seccion] = max(0, self.conteo[widget.seccion] - 1)

            self.canciones = [
                c for c in self.canciones if c["id"] != widget.cancion_data["id"]
            ]

            self.filas.remove(widget)

            self._solicitar_refresco()

    def limpiar_completadas(self):

        for card in [c for c in self.filas if c.seccion == "completadas"]:
            self.eliminar_item(card)

    # ========================================================
    # HILOS -> VENTANA
    # ========================================================

    def _llamar_en_ventana(self, funcion):
        """Pide que una función se ejecute en el hilo principal de la ventana."""
        self._cola_eventos.put(("llamar", funcion))

    def _drenar_cola_eventos(self):
        """Corre en el hilo principal: lee los avisos de los hilos y actualiza la UI."""

        try:
            ultimos_progreso = {}

            while True:

                try:
                    ev = self._cola_eventos.get_nowait()
                except queue.Empty:
                    break

                try:
                    tipo = ev[0]

                    if tipo == "llamar":
                        ev[1]()
                        continue

                    card, token = ev[1], ev[2]

                    if tipo == "progreso":
                        # Si llegan varios avances de la misma canción, nos quedamos con el último.
                        ultimos_progreso[card] = (token, ev[3])

                    elif tipo == "inicio":
                        self._evento_inicio(card, token)

                    elif tipo == "final":
                        ultimos_progreso.pop(card, None)
                        self._evento_final(card, token, ev[3], ev[4])

                    elif tipo == "omitido":
                        ultimos_progreso.pop(card, None)
                        self._trabajo_terminado()

                except Exception as e:
                    print(f"[GUI] Error procesando un evento: {e}")

            for card, (token, datos) in ultimos_progreso.items():
                try:
                    self._evento_progreso(card, token, datos)
                except Exception as e:
                    print(f"[GUI] Error actualizando progreso: {e}")

        except Exception as e:
            print(f"[GUI] Error leyendo la cola de eventos: {e}")

        finally:
            self.after(60, self._drenar_cola_eventos)

    # ========================================================
    # EVENTOS DE UNA CANCIÓN
    # ========================================================

    def _evento_inicio(self, card, token):

        if token != card.token or card not in self.filas:
            return

        card.encolada = False

        self._mover(card, "proceso")

        card.mostrar_detalle("")

        if card.fuente == "youtube":
            card.mostrar_busqueda("Buscando en YouTube Music...")
        elif card.manual:
            card.mostrar_busqueda("Solicitando descarga...")
        else:
            card.mostrar_busqueda("Buscando match...")

    def _evento_progreso(self, card, token, datos):

        if token != card.token or card not in self.filas:
            return

        if card.seccion != "proceso" or card.control.cancelar.is_set():
            return

        fase = datos.get("fase", "")

        if fase == "buscando":
            card.mostrar_busqueda(datos.get("estado") or "Buscando match...")

        elif fase == "descargando":
            card.actualizar_descarga(datos)

        if fase in ("buscando", "descargando") and "detalle" in datos:
            card.mostrar_detalle(datos.get("detalle"))

    def _evento_final(self, card, token, resultado, detalle):

        try:

            if token != card.token or card not in self.filas:
                return

            estado = (resultado or {}).get("estado", "error")

            if estado in ("completado", "existente"):

                archivo = (resultado or {}).get("archivo")

                info = (resultado or {}).get("resultado") or {}

                texto = Path(archivo).name if archivo else ""

                try:
                    bitrate = float(info.get("bitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0

                fuente = info.get("fuente")

                if texto and fuente:
                    # El MP3 queda a 320 kbps, pero el audio original de YouTube es de menor calidad.
                    texto += f"   ·   MP3 320 convertido de {fuente}"

                elif texto and bitrate > 0:
                    texto += f"   ·   {bitrate:.0f} kbps"

                if estado == "completado":
                    card.marcar_completado(texto)
                else:
                    card.marcar_existente(texto)

                self._mover(card, "completadas")

            elif estado == "sin_match":
                card.marcar_no_encontrado(detalle)
                self._mover(card, "atencion")

            elif estado == "cancelado":
                card.marcar_cancelado()
                self._mover(card, "atencion")

            else:
                card.marcar_error("Error", detalle)
                self._mover(card, "atencion")

        finally:
            self._trabajo_terminado()

    def _trabajo_terminado(self):

        self._trabajos = max(0, self._trabajos - 1)

        self._solicitar_refresco()

        if self._trabajos == 0 and self._resumen_pendiente:
            self._resumen_pendiente = False
            self.after(150, self._mostrar_resumen)

    def _mostrar_resumen(self):

        conteo = collections.Counter(
            c.cancion_data.get("estado") for c in self.filas
        )

        resumen = (
            "Se terminó de procesar la lista.\n\n"
            f"Descargadas: {conteo['Completado']}\n"
            f"Ya estaban descargadas: {conteo['Ya descargada']}\n"
            f"Sin resultados: {conteo['No encontrado']}\n"
            f"Canceladas: {conteo['Cancelado']}\n"
            f"Con error: {conteo['Error']}\n\n"
            "Las que no se pudieron descargar están en el filtro «Atención»: "
            "desde ahí puedes reintentar, buscar con otro texto o elegir un archivo a mano."
        )

        messagebox.showinfo("Proceso finalizado", resumen)

        if self._ajuste("abrir_carpeta_al_terminar"):
            self.abrir_explorador()

    # ========================================================
    # ACCIONES DE LAS FILAS
    # ========================================================

    def cancelar_item(self, widget):
        """Cancela de verdad: el downloader corta la descarga en slskd."""

        if widget not in self.filas:
            return

        widget.control.cancelar.set()

        # Los avisos que lleguen de este intento se ignoran.
        widget.token += 1

        widget.marcar_cancelado()

        self._mover(widget, "atencion")

    def siguiente_match_item(self, widget):
        """Abandona el match actual (por ejemplo, uno que quedó en cola) y prueba el siguiente."""

        widget.control.siguiente.set()

        widget.mostrar_busqueda("Buscando otro match...")

    def _al_cambiar_fallback(self):
        """Activa o desactiva el respaldo automático con YouTube Music."""

        quiere = bool(self.var_fallback_youtube.get())

        if quiere and self.downloader is not None:

            ok, mensaje = self.downloader.youtube_disponible()

            if not ok:

                self.var_fallback_youtube.set(False)

                messagebox.showwarning(
                    "YouTube Music no está listo",
                    mensaje,
                    parent=self,
                )

                quiere = False

        self.fallback_youtube = quiere

        if self.downloader is not None:
            self.downloader.ajustes.actualizar({"youtube_respaldo": quiere})

    def descargar_youtube_item(self, widget):
        """Descarga la canción de YouTube Music (MP3 320), sin pasar por Soulseek."""

        if widget not in self.filas or widget.seccion != "atencion":
            return

        if self.downloader is None:
            return

        ok, mensaje = self.downloader.youtube_disponible()

        if not ok:
            messagebox.showwarning("YouTube Music no está listo", mensaje, parent=self)
            return

        widget.manual = None
        widget.fuente = "youtube"

        self._encolar_varios([widget], urgente=True)

    def reintentar_item(self, widget):
        """Vuelve a buscar y descargar la canción desde cero."""

        if widget not in self.filas or widget.seccion != "atencion":
            return

        widget.manual = None
        widget.fuente = None

        self._encolar_varios([widget], urgente=True)

    def reintentar_todas(self):

        cards = [c for c in self.filas if c.seccion == "atencion"]

        if not cards:
            messagebox.showinfo("Nada que reintentar", "No hay canciones que requieran atención.")
            return

        for card in cards:
            card.manual = None
            card.fuente = None

        self._resumen_pendiente = True

        self._encolar_varios(cards, urgente=True)

    # ========================================================
    # SELECCIÓN MÚLTIPLE
    # ========================================================

    def _conteo_seleccion(self, cards=None):
        """Cuántas de las filas elegidas admiten cada acción."""

        cards = self.lista.seleccionados() if cards is None else cards

        return {
            "total": len(cards),
            "descargar": sum(1 for c in cards if c.seccion == "pendientes" and not c.encolada),
            "reintentar": sum(1 for c in cards if c.seccion == "atencion"),
            "cancelar": sum(1 for c in cards if c.seccion == "proceso" or c.encolada),
        }

    def _actualizar_barra_seleccion(self):

        try:

            cards = self.lista.seleccionados()

            cuenta = self._conteo_seleccion(cards)

            todo = self.lista.todo_seleccionado()

            firma = (cuenta["total"], cuenta["descargar"], cuenta["reintentar"], cuenta["cancelar"], todo)

            if firma == self._firma_barra:
                return

            self._firma_barra = firma

            self.btn_sel_todas.configure(
                text="Deseleccionar" if todo else "Seleccionar todas",
            )

            self.lbl_seleccion.pack_forget()
            self.frame_lote.pack_forget()

            for boton in self._botones_lote:
                boton.pack_forget()

            if not cuenta["total"]:
                return

            n = cuenta["total"]

            self.lbl_seleccion.configure(text=f"{n} seleccionada" + ("s" if n != 1 else ""))
            self.lbl_seleccion.pack(side="left", padx=(12, 8))

            if cuenta["descargar"]:
                self.btn_lote_descargar.pack(side="left", padx=(0, 6))

            if cuenta["reintentar"]:
                self.btn_lote_reintentar.pack(side="left", padx=(0, 6))
                self.btn_lote_youtube.pack(side="left", padx=(0, 6))

            if cuenta["cancelar"]:
                self.btn_lote_cancelar.pack(side="left", padx=(0, 6))

            self.btn_lote_quitar.pack(side="left", padx=(0, 6))
            self.btn_lote_soltar.pack(side="left")

            self.frame_lote.pack(side="left")

        except Exception as e:
            print(f"[GUI] Error actualizando la barra de selección: {e}")

    def alternar_seleccion_total(self):

        if self.lista.todo_seleccionado():
            self.lista.limpiar_seleccion()
        else:
            self.lista.seleccionar_todo()

    def _foco_en_texto(self):

        try:
            return isinstance(self.focus_get(), (tk.Entry, tk.Text))
        except Exception:
            return False

    def _atajo_seleccionar_todo(self, _event=None):

        if self._foco_en_texto():
            return None

        self.lista.seleccionar_todo()

        return "break"

    def _atajo_escape(self, _event=None):

        if self._foco_en_texto():
            return None

        self.lista.limpiar_seleccion()

        return None

    def _menu_lote(self, x, y):
        """Menú del clic derecho cuando hay varias canciones elegidas."""

        cuenta = self._conteo_seleccion()

        menu = menu_tema(self.lista.canvas)

        if cuenta["descargar"]:
            menu.add_command(label=f"Descargar ({cuenta['descargar']})", command=self.lote_descargar)

        if cuenta["reintentar"]:
            menu.add_command(label=f"Reintentar ({cuenta['reintentar']})", command=self.lote_reintentar)
            menu.add_command(
                label=f"Descargar de YouTube Music ({cuenta['reintentar']})", command=self.lote_youtube
            )

        if cuenta["cancelar"]:
            menu.add_command(label=f"Cancelar ({cuenta['cancelar']})", command=self.lote_cancelar)

        if menu.index("end") is not None:
            menu.add_separator()

        menu.add_command(label="Copiar nombres", command=self.lote_copiar)
        menu.add_command(label=f"Quitar de la lista ({cuenta['total']})", command=self.lote_quitar)
        menu.add_separator()
        menu.add_command(label="Deseleccionar", command=self.lista.limpiar_seleccion)

        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def lote_descargar(self):

        cards = [c for c in self.lista.seleccionados() if c.seccion == "pendientes" and not c.encolada]

        self.lista.limpiar_seleccion()

        self.iniciar_descargas(cards)

    def lote_reintentar(self):

        cards = [c for c in self.lista.seleccionados() if c.seccion == "atencion"]

        if not cards:
            return

        for card in cards:
            card.manual = None
            card.fuente = None

        self.lista.limpiar_seleccion()

        self._resumen_pendiente = True

        self._encolar_varios(cards, urgente=True)

    def lote_youtube(self):

        cards = [c for c in self.lista.seleccionados() if c.seccion == "atencion"]

        if not cards or self.downloader is None:
            return

        ok, mensaje = self.downloader.youtube_disponible()

        if not ok:
            messagebox.showwarning("YouTube Music no está listo", mensaje, parent=self)
            return

        for card in cards:
            card.manual = None
            card.fuente = "youtube"

        self.lista.limpiar_seleccion()

        self._resumen_pendiente = True

        self._encolar_varios(cards, urgente=True)

    def lote_cancelar(self):

        cards = [c for c in self.lista.seleccionados() if c.seccion == "proceso" or c.encolada]

        self.lista.limpiar_seleccion()

        for card in cards:
            card.encolada = False
            self.cancelar_item(card)

    def lote_quitar(self):

        cards = self.lista.seleccionados()

        if not cards:
            return

        if len(cards) > 1 and not messagebox.askyesno(
            "Quitar de la lista",
            f"Se van a quitar {len(cards)} canciones de la lista. Las que se estén descargando se cancelan "
            "(los archivos ya descargados no se borran). ¿Continuar?",
            parent=self,
        ):
            return

        self.lista.limpiar_seleccion()

        self.eliminar_varios(cards)

    def lote_copiar(self):

        cards = self.lista.seleccionados()

        try:
            self.clipboard_clear()
            self.clipboard_append(
                "\n".join(
                    f"{c.cancion_data.get('artista', '')} - {c.cancion_data.get('nombre', '')}" for c in cards
                )
            )
        except Exception:
            pass

    def eliminar_varios(self, cards):
        """Quita varias filas de una sola vez (mucho más rápido que de a una)."""

        quitar = {id(c) for c in cards if c in self.filas}

        if not quitar:
            return

        ids_cancion = set()

        for card in cards:

            if id(card) not in quitar:
                continue

            card.control.cancelar.set()
            card.token += 1
            card.encolada = False

            if card.seccion is not None:
                self.conteo[card.seccion] = max(0, self.conteo[card.seccion] - 1)

            ids_cancion.add(card.cancion_data["id"])

        self.filas[:] = [c for c in self.filas if id(c) not in quitar]

        self.canciones = [c for c in self.canciones if c["id"] not in ids_cancion]

        self._solicitar_refresco()

    # ========================================================
    # PAUSAR / CANCELAR TODO
    # ========================================================

    def pausar_o_reanudar(self):
        """Pausa: no empiezan canciones nuevas (las que ya van descargando terminan)."""

        self._asegurar_planificador()

        if self.pausado:
            self.pausado = False
            self.planificador.reanudar()
        else:
            self.pausado = True
            self.planificador.pausar()

        self._firma_pausa = None

        self._actualizar_resumen()

    def cancelar_todo(self):
        """Cancela lo que está descargando y lo que espera turno."""

        cards = [c for c in self.filas if c.seccion == "proceso" or c.encolada]

        if not cards:
            return

        if not messagebox.askyesno(
            "Cancelar todo",
            f"Se van a cancelar {len(cards)} canciones (en descarga o en espera). "
            "Los archivos incompletos se eliminan. ¿Continuar?",
            parent=self,
        ):
            return

        self._resumen_pendiente = False

        for card in cards:
            card.encolada = False
            self.cancelar_item(card)

        # Lo que seguía en la fila ya no se va a ejecutar.
        if self.planificador is not None:

            for _ in self.planificador.vaciar():
                self._trabajo_terminado()

        self._solicitar_refresco()

    def abrir_opciones(self, widget):
        """Abre la ventana con todo lo que devolvió la búsqueda de slskd."""

        if not self.app_lista or self.downloader is None:
            return

        try:
            ventana = getattr(widget, "_ventana", None)

            if ventana is not None and ventana.winfo_exists():
                ventana.deiconify()
                ventana.lift()
                ventana.focus()
                return

        except Exception:
            pass

        widget._ventana = VentanaResultados(self, widget)

    def descargar_elegidos(self, widget, elegidos):
        """Descarga los archivos que el usuario eligió a mano."""

        if widget not in self.filas:
            return

        widget.manual = list(elegidos)
        widget.fuente = None

        self._encolar_varios([widget], urgente=True)

        widget.mostrar_detalle(f"Descarga manual: {len(elegidos)} archivo(s) elegido(s)")

    # ========================================================
    # DESCARGAS
    # ========================================================

    def _asegurar_planificador(self):

        if self.planificador is None:
            simultaneas = int(self._ajuste("max_simultaneas") or MAX_SIMULTANEAS)

            self.planificador = Planificador(
                self._ejecutar_trabajo, simultaneas, max(MAX_EN_PROCESO, simultaneas * 2)
            )

    def _encolar_varios(self, cards, urgente=False):
        """Prepara las filas y las mete en la fila de trabajo."""

        self._asegurar_planificador()

        for card in cards:
            card.token += 1
            card.control = ControlDescarga()
            card.reiniciar_visual()
            card.mostrar_pendiente("En espera...")
            card.encolada = True
            self._mover(card, "pendientes")
            self._trabajos += 1

        # Con urgente se pasan al frente de la fila, conservando su orden.
        orden = list(reversed(cards)) if urgente else list(cards)

        for card in orden:
            self.planificador.encolar((card, card.token), urgente=urgente)

    def iniciar_descargas(self, solo=None):

        if not self.app_lista or self.downloader is None:
            return

        if self._preparando:
            return

        if solo is not None:

            if not solo:
                return

        elif not self.conteo["pendientes"]:
            messagebox.showwarning("Atención", "No hay canciones pendientes para descargar.")
            return

        self._solo_lote = solo

        self._preparando = True

        self.btn_descargar.configure(state="disabled", text="Preparando...")

        def preparar():

            ok = False

            try:
                ok = self.downloader.iniciar_servidor_slskd()

                # El historial solo se limpia si no hay otras descargas en curso.
                if ok and self._trabajos == 0:
                    # Limpia el historial viejo de slskd (terminadas, fallidas y en cola de otras veces)
                    try:
                        self.downloader.limpiar_historial_slskd()
                    except Exception as e:
                        print(f"[GUI] No se pudo limpiar el historial de slskd: {e}")

            except Exception as e:
                print(f"[GUI] Error preparando las descargas: {e}")
                ok = False

            self._llamar_en_ventana(lambda: self._arrancar_lote(ok))

        threading.Thread(target=preparar, daemon=True).start()

    def _arrancar_lote(self, ok):

        self._preparando = False

        self.btn_descargar.configure(state="normal", text=TEXTO_BOTON_DESCARGAR)

        if not ok:
            self._solo_lote = None
            messagebox.showerror(
                "Error de Conexión",
                "No se pudo conectar ni ejecutar slskd.exe. Mira la consola para ver el motivo.",
            )
            return

        origen = self._solo_lote if self._solo_lote is not None else self.filas

        self._solo_lote = None

        pendientes = [
            c for c in origen
            if c.seccion == "pendientes" and not c.encolada and c in self.filas
        ]

        if not pendientes:
            return

        # Si estaba en pausa, al pedir descargar se reanuda.
        if self.pausado:
            self.pausado = False

            if self.planificador is not None:
                self.planificador.reanudar()

        self._resumen_pendiente = True

        self._encolar_varios(pendientes, urgente=False)

    def _ejecutar_trabajo(self, trabajo, liberar=None):
        """Corre en un hilo de trabajo: busca y descarga una canción."""

        card, token = trabajo

        control = card.control

        if (
            token != card.token
            or card not in self.filas
            or control.cancelar.is_set()
        ):
            self._cola_eventos.put(("omitido", card, token))
            return

        self._cola_eventos.put(("inicio", card, token))

        ultimo_detalle = {"texto": ""}

        def callback(datos):

            # Apenas empieza a descargar, la siguiente canción ya puede buscar.
            if liberar is not None and (datos or {}).get("fase") == "descargando":
                liberar()

            if "detalle" in datos:
                ultimo_detalle["texto"] = datos.get("detalle") or ""

            self._cola_eventos.put(("progreso", card, token, dict(datos or {})))

        cancion = card.cancion_data

        artista = str(cancion.get("artista", "")).replace("\xa0", " ").strip()
        titulo = str(cancion.get("nombre", "")).replace("\xa0", " ").strip()

        album = str(cancion.get("album", "") or "")
        duracion_ms = int(cancion.get("duracion_ms", 0) or 0)

        try:

            if card.fuente == "youtube":

                resultado = self.downloader.procesar_youtube(
                    artista,
                    titulo,
                    album,
                    duracion_ms,
                    actualizar_progreso=callback,
                    control=control,
                )

            elif card.manual:

                resultado = self.downloader.procesar_resultados_manual(
                    card.manual,
                    actualizar_progreso=callback,
                    control=control,
                    artista=artista,
                    titulo=titulo,
                )

            elif not artista or not titulo:

                resultado = {"estado": "error"}

                ultimo_detalle["texto"] = "Faltan el artista o el título"

            else:

                resultado = self.downloader.procesar_cancion(
                    artista,
                    titulo,
                    actualizar_progreso=callback,
                    control=control,
                    detalles=card.detalles,
                )

                # No está en Soulseek: se prueba YouTube Music si el respaldo está activado.
                if (
                    self.fallback_youtube
                    and (resultado or {}).get("estado") in ("sin_match", "error")
                    and not control.cancelar.is_set()
                ):

                    motivo_soulseek = ultimo_detalle["texto"]

                    callback(
                        {
                            "fase": "buscando",
                            "estado": "Buscando en YouTube Music...",
                            "detalle": "No está en Soulseek: probando YouTube Music...",
                        }
                    )

                    resultado = self.downloader.procesar_youtube(
                        artista,
                        titulo,
                        album,
                        duracion_ms,
                        actualizar_progreso=callback,
                        control=control,
                    )

                    # Si tampoco está en YouTube, se muestran los dos motivos.
                    if (resultado or {}).get("estado") not in ("completado", "existente", "cancelado"):
                        ultimo_detalle["texto"] = (
                            f"Soulseek: {motivo_soulseek}   ·   YouTube: {ultimo_detalle['texto']}"
                        )

        except Exception as e:

            print(f"[GUI] Error descargando {artista} - {titulo}: {e}")

            resultado = {"estado": "error"}

            ultimo_detalle["texto"] = f"Error inesperado: {e}"

        self._cola_eventos.put(("final", card, token, resultado, ultimo_detalle["texto"]))


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    app = App()

    app.mainloop()
