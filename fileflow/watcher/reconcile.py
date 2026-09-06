"""Reconciliacion al arrancar.

Los eventos del sistema de archivos son efimeros: lo que llego mientras la
aplicacion estaba cerrada no genera ningun evento al abrirla. Sin este barrido,
Fileflow solo veria lo que pasa mientras esta en ejecucion, que no es como se
usa un ordenador de verdad.

Ver docs/watcher-y-seguridad.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..db.index import Index
from .stability import is_hidden, is_temp_file

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    new: list[Path] = field(default_factory=list)
    modified: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def total_seen(self) -> int:
        return len(self.new) + len(self.modified) + self.unchanged

    def summary(self) -> str:
        return (
            f"{self.total_seen} archivos en disco: "
            f"{len(self.new)} nuevos, {len(self.modified)} modificados, "
            f"{self.unchanged} sin cambios, {len(self.missing)} ya no estan"
        )


def scan_directory(directory: Path, recursive: bool = True) -> list[Path]:
    """Archivos procesables de un directorio, sin temporales ni ocultos."""
    pattern = "**/*" if recursive else "*"
    found: list[Path] = []
    for path in directory.glob(pattern):
        try:
            if not path.is_file():
                continue
            if is_temp_file(path) or is_hidden(path):
                continue
            found.append(path.resolve())
        except OSError as exc:
            log.debug("no se pudo acceder a %s: %s", path, exc)
    return found


def reconcile(index: Index, directories: list[Path] | None = None) -> ReconcileReport:
    """Compara el disco contra el indice y pone al dia la tabla `files`.

    | En disco | En indice | Accion                          |
    |----------|-----------|---------------------------------|
    | si       | no        | alta como 'pending'             |
    | si       | si, distinto tamano/mtime | reanalizar    |
    | si       | si, igual | solo actualizar last_seen       |
    | no       | si        | marcar 'missing'                |
    """
    if directories is None:
        directories = [Path(row["path"]) for row in index.list_watched_dirs()]

    report = ReconcileReport()
    indexed = index.all_indexed_paths()
    seen: set[str] = set()

    for directory in directories:
        if not directory.is_dir():
            log.warning("directorio vigilado inaccesible: %s", directory)
            continue

        for path in scan_directory(directory):
            key = str(path)
            seen.add(key)
            existing = index.get_file_by_path(path)

            try:
                stat = path.stat()
            except OSError:
                continue

            if existing is None:
                index.upsert_file(path)
                report.new.append(path)
            elif existing.size != stat.st_size or abs(existing.mtime - stat.st_mtime) > 1e-6:
                # upsert_file detecta el cambio, vuelve a 'pending' y descarta
                # los embeddings viejos.
                index.upsert_file(path)
                report.modified.append(path)
            else:
                index.upsert_file(path)
                report.unchanged += 1

    # Lo que estaba indexado dentro de los directorios vigilados y ya no esta.
    watched_prefixes = tuple(str(d) for d in directories)
    gone = [
        p
        for p in indexed - seen
        if p.startswith(watched_prefixes)
    ]
    if gone:
        index.mark_missing(gone)
        report.missing = gone

    log.info("reconciliacion: %s", report.summary())
    return report


def recover_interrupted(index: Index) -> list[dict[str, str]]:
    """Operaciones que quedaron a medias porque el proceso murio.

    Solo informa: la decision de reintentar o descartar es del usuario. Actuar
    solo sobre archivos en un estado desconocido es justo la clase de iniciativa
    que hace perder archivos.
    """
    interrupted = index.interrupted_operations()
    if not interrupted:
        return []

    findings = []
    for row in interrupted:
        src, dst = row["src"], row["dst"]
        src_exists = Path(src).exists() if src else False
        dst_exists = Path(dst).exists() if dst else False

        if dst_exists and not src_exists:
            state = "completada (el archivo esta en el destino)"
        elif src_exists and not dst_exists:
            state = "no llego a ejecutarse (el archivo sigue en el origen)"
        elif src_exists and dst_exists:
            state = "ambigua: el archivo esta en ambos sitios"
        else:
            state = "el archivo no esta en ninguno de los dos sitios"

        findings.append({"journal_id": str(row["id"]), "op": row["op"], "state": state,
                         "src": src or "", "dst": dst or ""})
        log.warning("operacion #%s interrumpida: %s", row["id"], state)

    return findings
