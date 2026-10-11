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
import urllib.request
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import customtkinter as ctk

from downloader import SoulseekDownloader, ControlDescarga, MAX_SIMULTANEAS
import actualizador


# ============================================================
# CONFIGURACIÓN VISUAL
# ============================================================

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

TEXTO_BOTON_DESCARGAR = "⚡ Descargar Playlist (320kbps MP3)"


# ============================================================
# PLANIFICADOR: reparte las canciones entre varios hilos
# ============================================================

class Planificador:
    """
    Cola de trabajos con N hilos. Permite meter trabajos nuevos (reintentos,
    descargas manuales) mientras otros ya están corriendo, y pasarlos al
    frente de la fila.
    """

    def __init__(self, ejecutar, cantidad_hilos):
        self._ejecutar = ejecutar
        self._cola = collections.deque()
        self._cond = threading.Condition()

        for _ in range(max(1, int(cantidad_hilos))):
            threading.Thread(target=self._bucle, daemon=True).start()

    def encolar(self, trabajo, urgente=False):
        with self._cond:
            if urgente:
                self._cola.appendleft(trabajo)
            else:
                self._cola.append(trabajo)
            self._cond.notify()

    def _bucle(self):
        while True:
            with self._cond:
                while not self._cola:
                    self._cond.wait()
                trabajo = self._cola.popleft()

            try:
                self._ejecutar(trabajo)
            except Exception as e:
                print(f"[GUI] Error en un hilo de trabajo: {e}")


# ============================================================
# VENTANA DE LOGIN
# ============================================================

class VentanaConfigSoulseek(ctk.CTkToplevel):
    """Pide el usuario y la contraseña de Soulseek, crea slskd.yml y conecta."""

    def __init__(self, parent, downloader, on_fin=None, primera_vez=False):
        super().__init__(parent)

        self.downloader = downloader
        self.on_fin = on_fin
        self._terminado = False
        self._ocupado = False

        self.title("Configurar Soulseek")
        self.geometry("440x430")
        self.resizable(False, False)
        self.configure(fg_color="#1e1f22")

        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        marco = ctk.CTkFrame(self, fg_color="#2b2d31", corner_radius=8)
        marco.pack(padx=16, pady=16, fill="both", expand=True)

        titulo = "¡Bienvenido! Falta un paso" if primera_vez else "Cuenta de Soulseek"

        ctk.CTkLabel(
            marco, text=titulo, font=("Segoe UI", 15, "bold"), text_color="#f2f3f5"
        ).pack(pady=(14, 4))

        ctk.CTkLabel(
            marco,
            text=(
                "Para buscar y descargar música, la app se conecta a Soulseek, una "
                "red gratuita.\nEscribe el usuario y la contraseña que quieras usar.\n"
                "Si el usuario todavía no existe, Soulseek lo crea automáticamente.\n"
                "Los datos se guardan solo en este equipo."
            ),
            font=("Segoe UI", 10),
            text_color="#b5bac1",
            justify="center",
            wraplength=380,
        ).pack(pady=(0, 10), padx=14)

        self.entry_user = ctk.CTkEntry(
            marco, placeholder_text="Usuario de Soulseek", height=34,
            fg_color="#1e1f22", border_color="#383a40", text_color="white",
        )
        self.entry_user.pack(fill="x", padx=20, pady=4)

        self.entry_pass = ctk.CTkEntry(
            marco, placeholder_text="Contraseña", show="•", height=34,
            fg_color="#1e1f22", border_color="#383a40", text_color="white",
        )
        self.entry_pass.pack(fill="x", padx=20, pady=4)

        self.var_ver = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            marco, text="Mostrar contraseña", variable=self.var_ver,
            command=lambda: self.entry_pass.configure(show="" if self.var_ver.get() else "•"),
            font=("Segoe UI", 10), text_color="#b5bac1",
        ).pack(anchor="w", padx=20, pady=(2, 6))

        self.lbl_estado = ctk.CTkLabel(
            marco, text="", font=("Segoe UI", 10), text_color="#b5bac1",
            justify="center", wraplength=380,
        )
        self.lbl_estado.pack(padx=14, pady=(0, 4))

        self.btn_guardar = ctk.CTkButton(
            marco, text="Guardar y conectar", height=36, fg_color="#5865f2",
            hover_color="#4752c4", font=("Segoe UI", 12, "bold"), command=self.guardar,
        )
        self.btn_guardar.pack(fill="x", padx=20, pady=(4, 14))

        try:
            previo = self.downloader.usuario_soulseek_configurado()
            if previo:
                self.entry_user.insert(0, previo)
        except Exception:
            pass

        self.after(150, self._al_frente)

    def _al_frente(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
            self.entry_user.focus_set()
        except Exception:
            pass

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
        usuario = self.entry_user.get().strip()
        clave = self.entry_pass.get()

        if not usuario or not clave:
            messagebox.showwarning(
                "Faltan datos", "Escribe el usuario y la contraseña.", parent=self
            )
            return

        self._ocupado = True
        self.btn_guardar.configure(state="disabled", text="Conectando...")
        self.lbl_estado.configure(
            text="Guardando y conectando con Soulseek (puede tardar hasta un minuto)...",
            text_color="#b5bac1",
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
        self._ocupado = False
        if ok:
            self._terminar(True)
            return
        self.btn_guardar.configure(state="normal", text="Guardar y conectar")
        self.lbl_estado.configure(text=mensaje, text_color="#ed4245")


class VentanaLogin(ctk.CTkToplevel):

    def __init__(self, parent, downloader, on_exito=None):
        super().__init__(parent)

        self.downloader = downloader
        self.on_exito = on_exito

        self.title("Iniciar sesión en slskd")
        self.geometry("360x280")
        self.resizable(False, False)
        self.configure(fg_color="#1e1f22")

        self.transient(parent)
        self.grab_set()

        self.frame = ctk.CTkFrame(self, fg_color="#2b2d31", corner_radius=8)
        self.frame.pack(padx=16, pady=16, fill="both", expand=True)

        self.lbl_titulo = ctk.CTkLabel(
            self.frame,
            text="Acceso a slskd",
            font=("Segoe UI", 14, "bold"),
            text_color="#f2f3f5",
        )
        self.lbl_titulo.pack(pady=(12, 4))

        self.lbl_sub = ctk.CTkLabel(
            self.frame,
            text="Usuario y contraseña de la web de slskd (por defecto: slskd / slskd)",
            font=("Segoe UI", 10),
            text_color="#b5bac1",
        )
        self.lbl_sub.pack(pady=(0, 12))

        self.entry_user = ctk.CTkEntry(
            self.frame,
            placeholder_text="Usuario",
            height=34,
            fg_color="#1e1f22",
            border_color="#383a40",
            text_color="white",
        )
        self.entry_user.pack(fill="x", padx=16, pady=4)

        self.entry_pass = ctk.CTkEntry(
            self.frame,
            placeholder_text="Contraseña",
            show="•",
            height=34,
            fg_color="#1e1f22",
            border_color="#383a40",
            text_color="white",
        )
        self.entry_pass.pack(fill="x", padx=16, pady=4)

        self.btn_guardar = ctk.CTkButton(
            self.frame,
            text="Guardar e Iniciar",
            height=34,
            fg_color="#5865f2",
            hover_color="#4752c4",
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
    marca con ✓ los que pasan el filtro automático. Permite buscar con otro
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
        self.geometry("1060x620")
        self.minsize(820, 460)
        self.configure(fg_color="#1e1f22")
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        # ----------------------------------------------------
        # CABECERA
        # ----------------------------------------------------

        self.frame_top = ctk.CTkFrame(self, fg_color="#2b2d31", corner_radius=8)
        self.frame_top.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkLabel(
            self.frame_top,
            text=f"{self.artista} — {self.titulo}",
            font=("Segoe UI", 14, "bold"),
            text_color="#f2f3f5",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 0))

        ctk.CTkLabel(
            self.frame_top,
            text=(
                "Estos son los archivos de audio que devolvió slskd, sin filtrar. "
                "Los marcados con ✓ son los que pasan el filtro automático. "
                "Elige uno o varios (Ctrl/Shift): si eliges varios se piden a la vez "
                "y te quedas con el que responda primero."
            ),
            font=("Segoe UI", 10),
            text_color="#b5bac1",
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
            fg_color="#1e1f22",
            border_color="#383a40",
            text_color="white",
            placeholder_text="Texto a buscar en slskd...",
        )
        self.entry_consulta.insert(0, consulta_inicial)
        self.entry_consulta.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry_consulta.bind("<Return>", lambda _e: self.buscar())

        self.btn_buscar = ctk.CTkButton(
            self.frame_busqueda,
            text="🔎 Buscar en slskd",
            width=140,
            height=32,
            fg_color="#5865f2",
            hover_color="#4752c4",
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
            fg_color="#1e1f22",
            border_color="#383a40",
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
            text_color="#f2f3f5",
        )
        self.chk_solo_ok.pack(side="right")

        # ----------------------------------------------------
        # TABLA
        # ----------------------------------------------------

        self.frame_tabla = ctk.CTkFrame(self, fg_color="#1e1f22")
        self.frame_tabla.pack(fill="both", expand=True, padx=12, pady=6)

        estilo = ttk.Style(self)

        try:
            estilo.theme_use("clam")
        except Exception:
            pass

        estilo.configure(
            "Res.Treeview",
            background="#1e1f22",
            fieldbackground="#1e1f22",
            foreground="#f2f3f5",
            rowheight=24,
            borderwidth=0,
        )
        estilo.configure(
            "Res.Treeview.Heading",
            background="#2b2d31",
            foreground="#f2f3f5",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )
        estilo.map(
            "Res.Treeview",
            background=[("selected", "#5865f2")],
            foreground=[("selected", "#ffffff")],
        )
        estilo.map("Res.Treeview.Heading", background=[("active", "#383a40")])

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
                minwidth=40,
                stretch=estirar,
                anchor="center" if clave in ("ok", "fmt", "kbps", "mb", "cola") else "w",
            )

        self.tree.tag_configure("ok", foreground="#7bd88f")
        self.tree.tag_configure("bloqueado", foreground="#6d6f78")

        self.scroll = ctk.CTkScrollbar(self.frame_tabla, command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scroll.set)

        self.scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<Double-1>", lambda _e: self.descargar())

        # ----------------------------------------------------
        # BARRA INFERIOR
        # ----------------------------------------------------

        self.frame_bottom = ctk.CTkFrame(self, fg_color="#2b2d31", corner_radius=8)
        self.frame_bottom.pack(fill="x", padx=12, pady=(6, 12))

        self.lbl_estado = ctk.CTkLabel(
            self.frame_bottom,
            text="",
            font=("Segoe UI", 10),
            text_color="#b5bac1",
            anchor="w",
        )
        self.lbl_estado.pack(side="left", fill="x", expand=True, padx=12, pady=10)

        self.btn_cerrar = ctk.CTkButton(
            self.frame_bottom,
            text="Cerrar",
            width=90,
            height=32,
            fg_color="#383a40",
            hover_color="#4e5058",
            command=self._cerrar,
        )
        self.btn_cerrar.pack(side="right", padx=(6, 12), pady=8)

        self.btn_descargar = ctk.CTkButton(
            self.frame_bottom,
            text="⬇ Descargar seleccionados",
            width=190,
            height=32,
            fg_color="#23a55a",
            hover_color="#1d8a4b",
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
            f"{len(self.pasan)} pasan el filtro automático (✓)"
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
                cola = "🔒 bloqueado"
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
                    "✓" if pasa else "",
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
        self.btn_buscar.configure(state="normal", text="🔎 Buscar en slskd")

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
        self.color = "#949ba4"
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

    def mostrar_pendiente(self, texto="⏳ En espera..."):
        self._mostrar_texto(texto, "#f0b232")
        self.cancion_data["estado"] = "Pendiente"

    def mostrar_busqueda(self, texto="🔎 Buscando match..."):
        self._mostrar_texto(texto, "#f0b232")

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
            texto, color = f"⬇️ Descargando{sufijo}", "#8ea1ff"
        elif "queued" in estado:
            texto, color = f"⏳ En cola{sufijo}", "#f0b232"
        else:
            texto, color = f"⏳ Solicitando{sufijo}", "#f0b232"

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
        self._mostrar_texto("✓ Completado", "#2ecc71")
        self.mostrar_detalle(detalle)
        self.cancion_data["estado"] = "Completado"

    def marcar_existente(self, detalle=""):
        self._mostrar_texto("⚠ Ya descargada", "#f0b232")
        self.mostrar_detalle(detalle)
        self.cancion_data["estado"] = "Ya descargada"

    def marcar_no_encontrado(self, detalle=None):
        self._mostrar_texto("✕ Sin match", "#ed4245", "accion")

        if detalle is not None:
            self.mostrar_detalle(detalle)

        self.cancion_data["estado"] = "No encontrado"

    def marcar_error(self, texto="❌ Error", detalle=None):
        self._mostrar_texto(texto, "#ed4245", "accion")

        if detalle is not None:
            self.mostrar_detalle(detalle)

        self.cancion_data["estado"] = "Error"

    def marcar_cancelado(self):
        self._mostrar_texto("✕ Cancelado", "#ed4245", "accion")
        self.mostrar_detalle("")
        self.cancion_data["estado"] = "Cancelado"


# ============================================================
# LISTA VIRTUAL
# ============================================================

class ListaVirtual(tk.Frame):
    """
    Lista de filas dibujada en UN solo canvas. Solo se dibujan las filas que se
    ven en pantalla (unas 25), sin importar cuántas canciones haya. Por eso
    redimensionar la ventana o hacer scroll es rápido aunque la lista sea larga.
    """

    FONDO = "#1e1f22"
    FILA = "#25272b"
    FILA_HOVER = "#2d2f34"

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

        self._tt_id = None
        self._tt_ventana = None
        self._tt_pos = (0, 0)

        self._cache_texto = {}

        # Escala de pantalla (por ejemplo 1.5 con Windows al 150%).
        try:
            self.esc = max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self.esc = 1.0

        e = self.e

        self.alto_fila = e(30)

        # Los tamaños van en píxeles (negativos), igual que en el resto de la app.
        self.f_titulo = tkfont.Font(family="Segoe UI", size=-e(11), weight="bold")
        self.f_artista = tkfont.Font(family="Segoe UI", size=-e(10))
        self.f_estado = tkfont.Font(family="Segoe UI", size=-e(10), weight="bold")
        self.f_detalle = tkfont.Font(family="Segoe UI", size=-e(9))
        self.f_boton = tkfont.Font(family="Segoe UI", size=-e(9), weight="bold")
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
            button_color="#2b2d31",
            button_hover_color="#383a40",
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
        self._col_estado = e(150)

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

    def set_items(self, items, resetear=False):

        self.items = items

        self._indice = {id(item): i for i, item in enumerate(items)}

        self._hover_item = None
        self._hover_hit = None

        if resetear:
            self.offset = 0

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
                    fill="#949ba4",
                    font=self.f_vacio,
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

    def _dibujar_fila(self, item, y):

        c = self.canvas
        e = self.e

        ancho = self._ancho
        alto = self.alto_fila

        y0 = y + e(1)
        y1 = y + alto - e(1)
        ymed = y + alto / 2

        hover = item is self._hover_item

        # Fondo y barra de color del estado
        self._rect_redondeado(
            e(2), y0, ancho - e(2), y1, e(4),
            fill=self.FILA_HOVER if hover else self.FILA,
            outline="",
        )

        c.create_rectangle(
            e(2), y0 + e(3), e(6), y1 - e(3),
            fill=item.color,
            outline="",
        )

        # ----------------------------------------------------
        # ZONA DERECHA: ✕, botones y progreso (de derecha a izquierda)
        # ----------------------------------------------------

        arriba = y0 + e(4)
        abajo = y1 - e(4)

        x2 = ancho - e(8)
        x1 = x2 - e(24)

        sobre_eliminar = self._hover_hit == ("eliminar", item)

        if sobre_eliminar:
            self._rect_redondeado(x1, arriba, x2, abajo, e(4), fill="#ed4245", outline="")

        c.create_text(
            (x1 + x2) / 2, ymed,
            text="✕",
            font=self.f_boton,
            fill="white" if sobre_eliminar else "#949ba4",
        )

        self._hits.append((x1, arriba, x2, abajo, "eliminar", item))

        limite = x1 - e(6)

        if item.modo == "accion":
            botones = (
                ("opciones", "📋 Opciones", e(82), "#383a40", "#4e5058"),
                ("reintentar", "🔄 Reintentar", e(86), "#5865f2", "#4752c4"),
                ("youtube", "▶ YouTube", e(76), "#c4302b", "#a52a25"),
            )

        elif item.modo == "progreso":
            botones = (
                ("cancelar", "Cancelar", e(62), "#ed4245", "#c03f42"),
                ("siguiente", "⏭ Otro", e(56), "#383a40", "#4e5058"),
            )

        else:
            botones = ()

        for nombre, texto, ancho_boton, color, color_hover in botones:

            bx2 = limite
            bx1 = bx2 - ancho_boton

            activo = self._hover_hit == (nombre, item)

            self._rect_redondeado(
                bx1, arriba, bx2, abajo, e(4),
                fill=color_hover if activo else color,
                outline="",
            )

            c.create_text((bx1 + bx2) / 2, ymed, text=texto, font=self.f_boton, fill="white")

            self._hits.append((bx1, arriba, bx2, abajo, nombre, item))

            limite = bx1 - e(4)

        if item.modo == "progreso":

            limite -= e(4)

            c.create_text(
                limite, ymed,
                text=f"{int(round(item.progreso_actual * 100))}%",
                font=self.f_detalle,
                fill="#b5bac1",
                anchor="e",
            )

            limite -= e(34) + e(4)

            ancho_barra = e(84)
            alto_barra = e(7)

            bx2 = limite
            bx1 = bx2 - ancho_barra

            c.create_rectangle(
                bx1, ymed - alto_barra / 2, bx2, ymed + alto_barra / 2,
                fill=self.FONDO,
                outline="",
            )

            relleno = max(0.0, min(1.0, item.progreso_actual))

            if relleno > 0:
                c.create_rectangle(
                    bx1, ymed - alto_barra / 2, bx1 + ancho_barra * relleno, ymed + alto_barra / 2,
                    fill="#5865f2",
                    outline="",
                )

            if abs(item.progreso_objetivo - item.progreso_actual) > 0.001:
                self._necesita_anim = True

            limite = bx1 - e(8)

        # ----------------------------------------------------
        # TEXTOS (título, artista, estado, detalle)
        # ----------------------------------------------------

        c.create_text(
            self._x_titulo, ymed,
            text=self._ajustar(item.cancion_data.get("nombre", ""), self._col_titulo, self.f_titulo),
            font=self.f_titulo,
            fill="#f2f3f5",
            anchor="w",
        )

        c.create_text(
            self._x_artista, ymed,
            text=self._ajustar(item.cancion_data.get("artista", ""), self._col_artista, self.f_artista),
            font=self.f_artista,
            fill="#b5bac1",
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
                fill="#949ba4",
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

        hit = self._hit_en(event.x, event.y)

        if not hit:
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

        menu = tk.Menu(
            self.canvas,
            tearoff=0,
            bg="#2b2d31",
            fg="#f2f3f5",
            activebackground="#5865f2",
            activeforeground="white",
            bd=0,
        )

        def agregar(texto, nombre):
            menu.add_command(
                label=texto,
                command=lambda: self.acciones[nombre](item),
            )

        if item.seccion == "atencion":
            agregar("🔄 Reintentar", "reintentar")
            agregar("📋 Ver opciones", "opciones")
            agregar("▶ Descargar de YouTube Music (MP3 320)", "youtube")

        elif item.seccion == "proceso":
            agregar("⏭ Otro match", "siguiente")
            agregar("Cancelar", "cancelar")

        if menu.index("end") is not None:
            menu.add_separator()

        menu.add_command(
            label="Copiar «artista - título»",
            command=lambda: self._copiar(item),
        )

        agregar("✕ Quitar de la lista", "eliminar")

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
                bg="#111214",
                fg="#f2f3f5",
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
        ("todas", "Todas", 96),
        ("proceso", "⬇ En proceso", 132),
        ("pendientes", "⏳ Pendientes", 132),
        ("completadas", "✓ Completadas", 142),
        ("atencion", "⚠ Atención", 122),
    )

    def __init__(self):
        super().__init__()

        self.title("Spotify & Soulseek Sync")
        self.geometry("1100x720")
        self.minsize(900, 520)
        self.configure(fg_color="#1e1f22")

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
        self.crear_pantalla_inicio()

        self.bind("<Configure>", self._evento_resize, add="+")

        self.after(60, self._drenar_cola_eventos)
        self.after(150, self.inicializar_backend)

    # ========================================================
    # CREAR INTERFAZ
    # ========================================================

    def crear_interfaz(self):

        self.frame_top = ctk.CTkFrame(self, fg_color="#2b2d31", corner_radius=8)
        self.frame_top.pack(pady=(12, 8), padx=16, fill="x")

        self.frame_title_row = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_title_row.pack(fill="x", padx=12, pady=(8, 2))

        self.label_titulo = ctk.CTkLabel(
            self.frame_title_row,
            text="Importar Playlist de Spotify",
            font=("Segoe UI", 14, "bold"),
            text_color="#5865f2",
        )
        self.label_titulo.pack(side="left")

        self.btn_login = ctk.CTkButton(
            self.frame_title_row,
            text="🔑 Configurar Soulseek",
            height=26,
            width=130,
            fg_color="#383a40",
            hover_color="#4e5058",
            text_color="white",
            font=("Segoe UI", 11),
            command=self.abrir_login,
            state="disabled",
        )
        self.btn_login.pack(side="right")

        self.btn_actualizar = ctk.CTkButton(
            self.frame_title_row,
            text=f"⟳ Actualizaciones (v{actualizador.VERSION})",
            height=26,
            width=170,
            fg_color="#383a40",
            hover_color="#4e5058",
            text_color="white",
            font=("Segoe UI", 11),
            command=lambda: self.buscar_actualizaciones(manual=True),
        )
        self.btn_actualizar.pack(side="right", padx=(0, 8))

        # ====================================================
        # BUSCADOR
        # ====================================================

        self.frame_search_row = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        self.frame_search_row.pack(fill="x", padx=12, pady=(4, 8))

        self.entry_url = ctk.CTkEntry(
            self.frame_search_row,
            placeholder_text="Pega el enlace de la playlist aquí...",
            height=34,
            fg_color="#1e1f22",
            border_color="#383a40",
            text_color="white",
        )
        self.entry_url.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.btn_importar = ctk.CTkButton(
            self.frame_search_row,
            text="Importar",
            height=34,
            width=100,
            fg_color="#5865f2",
            hover_color="#4752c4",
            text_color="white",
            font=("Segoe UI", 12, "bold"),
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
                height=26,
                corner_radius=13,
                fg_color="#2b2d31",
                hover_color="#383a40",
                text_color="#b5bac1",
                font=("Segoe UI", 11),
                command=lambda c=clave: self.cambiar_filtro(c),
            )
            boton.pack(side="left", padx=(0, 6))

            self.chips[clave] = boton

        self.entry_buscar = ctk.CTkEntry(
            self.frame_filtros,
            width=200,
            height=26,
            placeholder_text="🔍 Buscar en la lista...",
            fg_color="#1e1f22",
            border_color="#383a40",
            text_color="white",
            font=("Segoe UI", 11),
        )
        self.entry_buscar.pack(side="left", padx=(8, 0))
        self.entry_buscar.bind("<KeyRelease>", lambda _e: self._programar_busqueda())

        # Acciones que aparecen según haya canciones en cada grupo
        self.btn_reintentar_todas = ctk.CTkButton(
            self.frame_filtros,
            text="🔄 Reintentar todas",
            width=130,
            height=26,
            fg_color="#5865f2",
            hover_color="#4752c4",
            text_color="white",
            font=("Segoe UI", 11, "bold"),
            command=self.reintentar_todas,
        )

        self.btn_limpiar = ctk.CTkButton(
            self.frame_filtros,
            text="🧹 Limpiar completadas",
            width=160,
            height=26,
            fg_color="#383a40",
            hover_color="#4e5058",
            text_color="white",
            font=("Segoe UI", 11),
            command=self.limpiar_completadas,
        )

        # ====================================================
        # LISTA (virtual: solo dibuja las filas que se ven)
        # ====================================================

        self.frame_lista_container = ctk.CTkFrame(self, fg_color="#1e1f22", corner_radius=0)
        self.frame_lista_container.pack(pady=4, padx=16, fill="both", expand=True)

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
            mensaje_vacio="Pega una playlist para comenzar a cargar tus canciones 🎧",
        )
        self.lista.pack(fill="both", expand=True)

        # ====================================================
        # BARRA INFERIOR
        # ====================================================

        self.frame_bottom = ctk.CTkFrame(self, fg_color="#2b2d31", corner_radius=8)
        self.frame_bottom.pack(pady=(8, 12), padx=16, fill="x")

        self.btn_descargar = ctk.CTkButton(
            self.frame_bottom,
            text=TEXTO_BOTON_DESCARGAR,
            height=36,
            fg_color="#23a55a",
            hover_color="#1d8a4b",
            text_color="white",
            font=("Segoe UI", 12, "bold"),
            command=self.iniciar_descargas,
            state="disabled",
        )
        self.btn_descargar.pack(side="left", padx=12, pady=8)

        self.btn_explorador = ctk.CTkButton(
            self.frame_bottom,
            text="📁 Mostrar en Explorador",
            height=36,
            fg_color="#383a40",
            hover_color="#4e5058",
            text_color="white",
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
            text_color="#b5bac1",
            checkbox_width=18,
            checkbox_height=18,
        )
        self.chk_youtube.pack(side="right", padx=(0, 4), pady=8)

        self.frame_global = ctk.CTkFrame(self.frame_bottom, fg_color="transparent")
        self.frame_global.pack(side="left", fill="x", expand=True, padx=8)

        self.lbl_global = ctk.CTkLabel(
            self.frame_global,
            text="",
            font=("Segoe UI", 11),
            text_color="#b5bac1",
            anchor="w",
        )
        self.lbl_global.pack(fill="x")

        self.progress_global = ctk.CTkProgressBar(
            self.frame_global,
            height=6,
            fg_color="#1e1f22",
            progress_color="#23a55a",
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

        self.frame_inicio = ctk.CTkFrame(self, fg_color="#1e1f22", corner_radius=0)
        self.frame_inicio.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.frame_inicio_centro = ctk.CTkFrame(self.frame_inicio, fg_color="transparent")
        self.frame_inicio_centro.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_inicio_titulo = ctk.CTkLabel(
            self.frame_inicio_centro,
            text="Spotify & Soulseek Sync",
            font=("Segoe UI", 24, "bold"),
            text_color="#f2f3f5",
        )
        self.lbl_inicio_titulo.pack(pady=(0, 8))

        self.lbl_inicio_estado = ctk.CTkLabel(
            self.frame_inicio_centro,
            text="Iniciando aplicación...",
            font=("Segoe UI", 13),
            text_color="#b5bac1",
        )
        self.lbl_inicio_estado.pack(pady=(0, 18))

        self.progress_inicio = ctk.CTkProgressBar(
            self.frame_inicio_centro,
            width=300,
            height=8,
            fg_color="#2b2d31",
            progress_color="#5865f2",
        )
        self.progress_inicio.set(0)
        self.progress_inicio.pack()

        self.lbl_inicio_detalle = ctk.CTkLabel(
            self.frame_inicio_centro,
            text="Preparando...",
            font=("Segoe UI", 10),
            text_color="#949ba4",
        )
        self.lbl_inicio_detalle.pack(pady=(10, 0))

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
                        evento.set()

                    self._llamar_en_ventana(
                        lambda: VentanaConfigSoulseek(
                            self, self.downloader, on_fin=al_terminar, primera_vez=True
                        )
                    )

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
        self.btn_importar.configure(state="normal")
        self.btn_descargar.configure(state="normal")
        self.btn_explorador.configure(state="normal")

        self.after(400, self.ocultar_pantalla_inicio)

        if not getattr(self, "_actualizaciones_comprobadas", False):
            self._actualizaciones_comprobadas = True
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

        self.lbl_inicio_estado.configure(text="No se pudo iniciar", text_color="#ed4245")
        self.lbl_inicio_detalle.configure(
            text="Revisa la consola o el archivo slskd_arranque.log."
        )

        self.progress_inicio.set(max(0.0, min(1.0, self.inicio_progreso_real)))

        self.after(
            300,
            lambda: messagebox.showerror("Error de inicialización", texto, parent=self),
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

        try:
            if self.frame_inicio.winfo_exists():
                self.frame_inicio.place_forget()
                self.frame_inicio.destroy()
        except Exception:
            pass

    # ========================================================
    # LOGIN / EXPLORADOR
    # ========================================================

    def configuracion_pendiente(self):
        """El usuario cerró la ventana de Soulseek sin configurar nada."""

        self.app_lista = False

        self.lbl_inicio_estado.configure(text="Falta configurar Soulseek", text_color="#faa61a")
        self.lbl_inicio_detalle.configure(
            text="Cierra la aplicación y ábrela de nuevo para escribir tu cuenta de Soulseek."
        )
        self.progress_inicio.set(0.25)

        messagebox.showinfo(
            "Falta configurar Soulseek",
            "Sin una cuenta de Soulseek la app no puede buscar ni descargar música.\n\n"
            "Cierra la aplicación y ábrela de nuevo para configurarla.",
            parent=self,
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

    def _reiniciar_app(self):

        try:
            if getattr(sys, "frozen", False):
                comando = [sys.executable]
            else:
                comando = [sys.executable] + sys.argv

            subprocess.Popen(comando, cwd=str(self._carpeta_programa()))
        except Exception as e:
            messagebox.showinfo(
                "Actualización instalada",
                f"La actualización se instaló. Cierra y vuelve a abrir la app.\n\n({e})",
                parent=self,
            )
            return

        self.destroy()
        os._exit(0)

    def abrir_explorador(self):

        if not self.app_lista or self.downloader is None:
            return

        self.downloader.abrir_en_explorador()

    # ========================================================
    # IMPORTAR PLAYLIST
    # ========================================================

    def importar_playlist(self):

        if not self.app_lista:
            return

        url = self.entry_url.get().strip()

        if not url:
            messagebox.showwarning(
                "Atención", "Por favor ingresa un enlace válido de Spotify."
            )
            return

        self.btn_importar.configure(state="disabled", text="Cargando...")

        def tarea():
            try:
                match = re.search(r"playlist/([a-zA-Z0-9]+)", url)

                if not match:
                    self._llamar_en_ventana(
                        lambda: messagebox.showerror("Error", "URL de playlist inválida.")
                    )
                    return

                playlist_id = match.group(1)

                embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"

                req = urllib.request.Request(embed_url, headers=HEADERS)

                html = urllib.request.urlopen(req).read().decode("utf-8")

                match_json = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    html,
                )

                if not match_json:
                    raise Exception("No se encontró la información de la playlist.")

                data = json.loads(match_json.group(1))

                track_list = data["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]

                canciones_nuevas = []

                for i, t in enumerate(track_list, 1):
                    artista = str(t.get("subtitle", "")).replace("\xa0", " ").strip()
                    nombre = str(t.get("title", "")).replace("\xa0", " ").strip()
                    album = t.get("entity", {}).get("release", {}).get("title", "Álbum")

                    canciones_nuevas.append({
                        "id": i,
                        "nombre": nombre,
                        "artista": artista,
                        "album": album,
                        "duracion_ms": self._numero(t.get("duration")),
                        "busqueda": f"{artista} - {nombre}",
                        "estado": "Pendiente",
                    })

                self._llamar_en_ventana(
                    lambda lista=canciones_nuevas: self._cargar_playlist_ui(lista)
                )

            except Exception as e:
                self._llamar_en_ventana(
                    lambda err=e: messagebox.showerror(
                        "Error", f"No se pudo procesar la playlist:\n{err}"
                    )
                )

            finally:
                self._llamar_en_ventana(self.reset_btn_importar)

        threading.Thread(target=tarea, daemon=True).start()

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
            self.lista.set_mensaje_vacio("Pega una playlist para comenzar a cargar tus canciones 🎧")

        self._actualizar_resumen()

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
                fondo = "#c03f42" if clave == "atencion" else "#5865f2"
                color_texto = "white"

            else:
                fondo = "#2b2d31"
                color_texto = "#ed4245" if (clave == "atencion" and cantidad) else "#b5bac1"

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

        # Progreso general
        if total:
            hechas = n["completadas"]
            self.progress_global.set(hechas / total)
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

        self._mover(card, "proceso")

        card.mostrar_detalle("")

        if card.fuente == "youtube":
            card.mostrar_busqueda("🔎 Buscando en YouTube Music...")
        elif card.manual:
            card.mostrar_busqueda("⏳ Solicitando descarga...")
        else:
            card.mostrar_busqueda("🔎 Buscando match...")

    def _evento_progreso(self, card, token, datos):

        if token != card.token or card not in self.filas:
            return

        if card.seccion != "proceso" or card.control.cancelar.is_set():
            return

        fase = datos.get("fase", "")

        if fase == "buscando":
            card.mostrar_busqueda(datos.get("estado") or "🔎 Buscando match...")

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
                card.marcar_error("❌ Error", detalle)
                self._mover(card, "atencion")

        finally:
            self._trabajo_terminado()

    def _trabajo_terminado(self):

        self._trabajos = max(0, self._trabajos - 1)

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

        widget.mostrar_busqueda("🔄 Buscando otro match...")

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
            self.planificador = Planificador(self._ejecutar_trabajo, MAX_SIMULTANEAS)

    def _encolar_varios(self, cards, urgente=False):
        """Prepara las filas y las mete en la fila de trabajo."""

        self._asegurar_planificador()

        for card in cards:
            card.token += 1
            card.control = ControlDescarga()
            card.reiniciar_visual()
            card.mostrar_pendiente("⏳ En espera...")
            self._mover(card, "pendientes")
            self._trabajos += 1

        # Con urgente se pasan al frente de la fila, conservando su orden.
        orden = list(reversed(cards)) if urgente else list(cards)

        for card in orden:
            self.planificador.encolar((card, card.token), urgente=urgente)

    def iniciar_descargas(self):

        if not self.app_lista or self.downloader is None:
            return

        if self._preparando:
            return

        if self._trabajos > 0:
            messagebox.showinfo(
                "Descargas en curso",
                "Ya hay descargas en curso. Espera a que terminen o cancélalas.",
            )
            return

        if not self.conteo["pendientes"]:
            messagebox.showwarning("Atención", "No hay canciones pendientes para descargar.")
            return

        self._preparando = True

        self.btn_descargar.configure(state="disabled", text="⏳ Preparando...")

        def preparar():

            ok = False

            try:
                ok = self.downloader.iniciar_servidor_slskd()

                if ok:
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
            messagebox.showerror(
                "Error de Conexión",
                "No se pudo conectar ni ejecutar slskd.exe. Mira la consola para ver el motivo.",
            )
            return

        pendientes = [c for c in self.filas if c.seccion == "pendientes"]

        if not pendientes:
            return

        self._resumen_pendiente = True

        self._encolar_varios(pendientes, urgente=False)

    def _ejecutar_trabajo(self, trabajo):
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
                            "estado": "🔎 Buscando en YouTube Music...",
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