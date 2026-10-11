"""
Punto de entrada del programa empaquetado (.exe).

Si existe la carpeta "actualizacion" junto al programa, usa esos archivos .py
(versión actualizada). Si fallan al cargar, vuelve a la versión del instalador.
"""

import importlib.util
import sys
import traceback
from pathlib import Path

MODULOS = ("actualizador", "downloader", "youtube", "gui")


def carpeta_programa():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def cargar_desde(carpeta):
    """Carga los módulos desde archivos .py concretos. Lanza error si algo falla."""

    anteriores = {n: sys.modules.get(n) for n in MODULOS}

    try:
        for nombre in MODULOS:

            ruta = carpeta / f"{nombre}.py"

            if not ruta.exists():
                # No viene en la actualización: se usa el del instalador.
                __import__(nombre)
                continue

            spec = importlib.util.spec_from_file_location(nombre, ruta)
            modulo = importlib.util.module_from_spec(spec)
            sys.modules[nombre] = modulo
            spec.loader.exec_module(modulo)

    except Exception:
        for nombre, modulo in anteriores.items():
            if modulo is None:
                sys.modules.pop(nombre, None)
            else:
                sys.modules[nombre] = modulo
        raise

    return sys.modules["gui"]


def principal():

    base = carpeta_programa()
    carpeta = base / "actualizacion"

    gui = None

    if carpeta.exists() and any(carpeta.glob("*.py")):

        try:
            gui = cargar_desde(carpeta)
            print(f"[LANZADOR] Usando archivos actualizados de {carpeta}")

        except Exception:
            print("[LANZADOR] La actualización falló al cargar; se usa la versión original.")
            traceback.print_exc()

            # Se aparta la actualización defectuosa para no repetir el error.
            try:
                destino = base / "actualizacion_fallida"
                if destino.exists():
                    import shutil
                    shutil.rmtree(destino, ignore_errors=True)
                carpeta.rename(destino)
            except Exception:
                pass

            gui = None

    if gui is None:
        # Estas importaciones también hacen que PyInstaller incluya los módulos.
        import actualizador  # noqa: F401
        import downloader  # noqa: F401
        import youtube  # noqa: F401
        import gui

    gui.App().mainloop()


if __name__ == "__main__":
    principal()