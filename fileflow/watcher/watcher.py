"""Vigilancia de directorios con debounce.

Los eventos del sistema de archivos no se procesan segun llegan: se acumulan en
un diccionario `ruta -> ultimo evento` y un hilo trabajador emite una ruta
cuando lleva N segundos en silencio Y el archivo esta estable. Los cincuenta
eventos de una descarga se colapsan en un solo trabajo.

Ver docs/watcher-y-seguridad.md
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from ..config import WatcherSettings
from .stability import is_stable, is_temp_file, should_process

log = logging.getLogger(__name__)

# Callback que recibe cada archivo listo para procesar.
FileCallback = Callable[[Path], None]


@dataclass
class _Pending:
    path: Path
    last_event: float
    attempts: int = 0


class _EventCollector(FileSystemEventHandler):
    """Manejador de watchdog. Solo anota; no hace trabajo pesado.

    El hilo de eventos de watchdog debe volver rapido: cualquier espera aqui
    hace que se pierdan eventos.
    """

    def __init__(self, queue: _DebounceQueue):
        self.queue = queue

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.queue.touch(Path(str(event.src_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.queue.touch(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Un renombrado llega como 'moved'. Interesa el destino: es el archivo
        # que existe ahora.
        if not event.is_directory:
            self.queue.touch(Path(str(event.dest_path)))


class _DebounceQueue:
    """Acumula rutas y las emite cuando se quedan quietas."""

    def __init__(self, settings: WatcherSettings, on_ready: FileCallback):
        self.settings = settings
        self.on_ready = on_ready
        self._pending: dict[Path, _Pending] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ignored: dict[Path, float] = {}

    # -- entrada -------------------------------------------------------------

    def touch(self, path: Path) -> None:
        """Anota actividad sobre una ruta. Barato: se llama muchas veces."""
        if is_temp_file(path):
            return
        with self._lock:
            if self._is_self_write(path):
                return
            entry = self._pending.get(path)
            if entry:
                entry.last_event = time.monotonic()
            else:
                self._pending[path] = _Pending(path=path, last_event=time.monotonic())

    def ignore_briefly(self, path: Path, seconds: float = 5.0) -> None:
        """Ignora una ruta durante un rato.

        Lo usa el executor cuando el propio Fileflow acaba de escribir en una
        carpeta vigilada, para no reprocesar su propia escritura en bucle.
        """
        with self._lock:
            self._ignored[path] = time.monotonic() + seconds

    def _is_self_write(self, path: Path) -> bool:
        until = self._ignored.get(path)
        if until is None:
            return False
        if time.monotonic() > until:
            del self._ignored[path]
            return False
        return True

    # -- bucle ---------------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            self._drain()
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()

    def _drain(self) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [
                e
                for e in self._pending.values()
                if now - e.last_event >= self.settings.debounce_seconds
            ]
            for entry in ready:
                self._pending.pop(entry.path, None)

        for entry in ready:
            self._process(entry)

    def _process(self, entry: _Pending) -> None:
        path = entry.path
        try:
            if not path.exists():
                return
            if not should_process(path):
                return

            if not is_stable(
                path,
                checks=self.settings.stability_checks,
                interval=self.settings.stability_interval,
            ):
                # Sigue escribiendose. Una ISO de 8 GB puede tardar media hora:
                # se reprograma, no se descarta.
                entry.attempts += 1
                if entry.attempts <= self.settings.max_retries:
                    with self._lock:
                        entry.last_event = time.monotonic()
                        self._pending[path] = entry
                else:
                    log.warning("%s sigue inestable tras %d intentos", path, entry.attempts)
                return

            self.on_ready(path)
        except OSError as exc:
            log.warning("no se pudo procesar %s: %s", path, exc)


class FileWatcher:
    """Vigila varios directorios y llama a `on_file` con cada archivo estable.

        watcher = FileWatcher(settings, on_file=procesar)
        watcher.add_directory(Path.home() / "Downloads")
        watcher.start()
    """

    def __init__(self, settings: WatcherSettings, on_file: FileCallback):
        self.settings = settings
        self.queue = _DebounceQueue(settings, on_file)
        self.observer = Observer()
        self.handler = _EventCollector(self.queue)
        self.directories: list[Path] = []
        self._worker: threading.Thread | None = None

    def add_directory(self, path: str | Path, recursive: bool = True) -> None:
        directory = Path(path).resolve()
        if not directory.is_dir():
            raise NotADirectoryError(f"no es un directorio: {directory}")

        # Directorios solapados generan eventos duplicados para el mismo archivo.
        for existing in self.directories:
            if directory == existing or directory.is_relative_to(existing):
                log.warning("%s ya esta cubierto por %s, se omite", directory, existing)
                return

        self.observer.schedule(self.handler, str(directory), recursive=recursive)
        self.directories.append(directory)

    def start(self) -> None:
        if not self.directories:
            raise RuntimeError("no hay directorios que vigilar")
        self._worker = threading.Thread(target=self.queue.run, daemon=True, name="fileflow-debounce")
        self._worker.start()
        self.observer.start()
        log.info("vigilando %d directorio(s)", len(self.directories))

    def stop(self) -> None:
        self.queue.stop()
        self.observer.stop()
        self.observer.join(timeout=5)
        if self._worker:
            self._worker.join(timeout=5)

    def __enter__(self) -> FileWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
