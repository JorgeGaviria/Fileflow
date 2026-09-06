"""Deteccion de archivos estables.

Un evento del sistema de archivos NO significa que el archivo este completo: el
navegador dispara decenas de eventos mientras escribe una descarga. Tocar un
archivo a medio escribir es leer basura o, peor, moverlo mientras otro proceso
escribe en el.

Ver docs/watcher-y-seguridad.md
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Extensiones de archivo temporal o de descarga en curso. Nunca se procesan.
TEMP_EXTENSIONS = frozenset(
    {
        ".crdownload",  # Chrome / Edge
        ".part",  # Firefox
        ".partial",
        ".download",  # Safari
        ".opdownload",  # Opera
        ".!ut",  # uTorrent
        ".tmp",
        ".temp",
        ".swp",
        ".bak",
    }
)

# Prefijos de nombre a ignorar.
TEMP_PREFIXES = ("~$", ".~", "~")


def is_temp_file(path: Path) -> bool:
    """Archivo temporal, de bloqueo o descarga en curso, solo por el nombre.

    Estrictamente temporales: los ocultos (.gitignore y similares) son otra
    categoria y los cubre is_hidden().
    """
    if path.suffix.lower() in TEMP_EXTENSIONS:
        return True
    return path.name.startswith(TEMP_PREFIXES)


def is_hidden(path: Path) -> bool:
    """Oculto: por nombre (estilo Unix) o por atributo (Windows).

    Los dotfiles se descartan tambien en Windows aunque no lleven el atributo:
    .gitignore o .env son configuracion, no contenido que el usuario quiera ver
    reorganizado.
    """
    if path.name.startswith("."):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return False
        FILE_ATTRIBUTE_HIDDEN = 0x2
        FILE_ATTRIBUTE_SYSTEM = 0x4
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
    except Exception:
        return False


def is_locked(path: Path) -> bool:
    """Otro proceso tiene el archivo abierto para escritura.

    En Windows abrir en modo exclusivo falla si alguien mas lo tiene abierto,
    y esa es la senal mas fiable de las tres: el tamano puede estar quieto
    varios segundos en una descarga lenta, pero el bloqueo no miente.
    """
    try:
        with open(path, "rb+"):
            return False
    except PermissionError:
        return True
    except OSError:
        # No existe, o no hay permisos: no es un bloqueo, que lo resuelva el
        # que llama.
        return False


def is_stable(
    path: Path,
    checks: int = 2,
    interval: float = 1.0,
) -> bool:
    """True si el archivo lleva `checks` muestras sin cambiar de tamano ni mtime.

    Bloquea durante aproximadamente `(checks - 1) * interval` segundos, asi que
    debe llamarse desde el hilo trabajador, nunca desde el manejador de eventos.
    """
    if not path.exists() or not path.is_file():
        return False
    if is_temp_file(path):
        return False

    try:
        previous = path.stat()
    except OSError:
        return False

    for _ in range(max(0, checks - 1)):
        time.sleep(interval)
        try:
            current = path.stat()
        except OSError:
            return False
        if current.st_size != previous.st_size or current.st_mtime != previous.st_mtime:
            return False
        previous = current

    return not is_locked(path)


def should_process(path: Path) -> bool:
    """Filtro rapido previo, sin esperas. Descarta lo que no se procesa nunca."""
    return (
        path.is_file()
        and not is_temp_file(path)
        and not is_hidden(path)
        and path.stat().st_size > 0
    )
