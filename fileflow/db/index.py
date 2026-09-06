"""Capa de indice: todo el acceso a SQLite pasa por aqui.

Ninguna otra parte del codigo escribe SQL. Eso mantiene en un solo sitio las dos
invariantes que importan:

  1. Un vector NUNCA se lee ni se compara sin filtrar por (model_id, kind).
     Vectores de modelos distintos no dan un resultado malo, dan un resultado
     sin sentido.
  2. Toda operacion sobre el sistema de archivos queda registrada en el journal
     ANTES de ejecutarse.

Ver docs/esquema-de-datos.md
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------------------------------------------------------------------------
# Serializacion de vectores
# ---------------------------------------------------------------------------


def pack_vector(vec: np.ndarray) -> bytes:
    """float32 little-endian, normalizado a norma 1.

    Se normaliza al guardar para que la similitud coseno sea despues un simple
    producto escalar. Es la operacion mas repetida del sistema: conviene que sea
    lo mas barata posible.
    """
    v = np.asarray(vec, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(v))
    if norm > 0:
        v = v / norm
    return v.astype("<f4").tobytes()


def unpack_vector(blob: bytes, dim: int) -> np.ndarray:
    vec = np.frombuffer(blob, dtype="<f4")
    if vec.size != dim:
        raise ValueError(f"vector corrupto: se esperaban {dim} dimensiones, hay {vec.size}")
    return vec


# ---------------------------------------------------------------------------
# Registros
# ---------------------------------------------------------------------------


@dataclass
class FileRecord:
    id: int
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    status: str
    content_hash: str | None = None
    folder_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> FileRecord:
        return cls(
            id=row["id"],
            path=row["path"],
            name=row["name"],
            ext=row["ext"],
            size=row["size"],
            mtime=row["mtime"],
            status=row["status"],
            content_hash=row["content_hash"],
            folder_id=row["folder_id"],
        )


@dataclass
class FolderRecord:
    id: int
    path: str
    description: str
    enabled: bool
    auto_move: bool
    is_trash: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> FolderRecord:
        return cls(
            id=row["id"],
            path=row["path"],
            description=row["description"],
            enabled=bool(row["enabled"]),
            auto_move=bool(row["auto_move"]),
            is_trash=bool(row["is_trash"]),
        )


# ---------------------------------------------------------------------------
# Indice
# ---------------------------------------------------------------------------


class Index:
    """Acceso al indice SQLite.

    Uso:
        with Index(path) as idx:
            idx.upsert_file(...)
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL permite que el watcher escriba mientras la UI lee.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # -- esquema -------------------------------------------------------------

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        current = self.get_meta("schema_version")
        if current is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(current) != SCHEMA_VERSION:
            # Mientras el proyecto este en desarrollo temprano la via soportada
            # es borrar .fileflow/ y reindexar: el indice es una cache
            # reconstruible. Ver docs/esquema-de-datos.md
            raise RuntimeError(
                f"El indice es version {current} y el codigo espera {SCHEMA_VERSION}. "
                f"Borra {self.db_path.parent} y vuelve a indexar."
            )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- directorios vigilados ----------------------------------------------

    def add_watched_dir(self, path: str | Path, recursive: bool = True) -> int:
        p = str(Path(path).resolve())
        cur = self.conn.execute(
            "INSERT INTO watched_dirs (path, recursive) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET enabled = 1, recursive = excluded.recursive",
            (p, int(recursive)),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM watched_dirs WHERE path = ?", (p,)).fetchone()
        return row["id"]

    def list_watched_dirs(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM watched_dirs"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return list(self.conn.execute(sql + " ORDER BY path"))

    # -- carpetas destino ----------------------------------------------------

    def add_folder(
        self,
        path: str | Path,
        description: str = "",
        is_trash: bool = False,
    ) -> int:
        p = str(Path(path).resolve())
        cur = self.conn.execute(
            "INSERT INTO folders (path, description, is_trash) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  description = excluded.description, "
            "  is_trash = excluded.is_trash, "
            "  updated_at = datetime('now')",
            (p, description, int(is_trash)),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM folders WHERE path = ?", (p,)).fetchone()
        return row["id"]

    def list_folders(self, enabled_only: bool = True) -> list[FolderRecord]:
        sql = "SELECT * FROM folders"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return [FolderRecord.from_row(r) for r in self.conn.execute(sql + " ORDER BY path")]

    def get_trash_folder(self) -> FolderRecord | None:
        row = self.conn.execute("SELECT * FROM folders WHERE is_trash = 1 LIMIT 1").fetchone()
        return FolderRecord.from_row(row) if row else None

    # -- archivos ------------------------------------------------------------

    def upsert_file(self, path: str | Path, status: str = "pending") -> int:
        """Da de alta o actualiza un archivo a partir de su estado en disco.

        Si el archivo ya existia y cambio de tamano o mtime, vuelve a 'pending'
        para que se reanalice: su contenido ya no es el que se indexo.
        """
        p = Path(path)
        stat = p.stat()
        resolved = str(p.resolve())

        existing = self.get_file_by_path(resolved)
        if existing:
            changed = existing.size != stat.st_size or abs(existing.mtime - stat.st_mtime) > 1e-6
            new_status = "pending" if changed else existing.status
            self.conn.execute(
                "UPDATE files SET size = ?, mtime = ?, status = ?, last_seen = datetime('now') "
                "WHERE id = ?",
                (stat.st_size, stat.st_mtime, new_status, existing.id),
            )
            if changed:
                # El contenido cambio: los embeddings viejos ya no describen
                # este archivo.
                self.conn.execute("DELETE FROM embeddings WHERE file_id = ?", (existing.id,))
            self.conn.commit()
            return existing.id

        cur = self.conn.execute(
            "INSERT INTO files (path, name, ext, size, mtime, status) VALUES (?, ?, ?, ?, ?, ?)",
            (resolved, p.name, p.suffix.lower(), stat.st_size, stat.st_mtime, status),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_file_by_path(self, path: str | Path) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (str(Path(path).resolve()),)
        ).fetchone()
        return FileRecord.from_row(row) if row else None

    def get_file(self, file_id: int) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return FileRecord.from_row(row) if row else None

    def iter_files(self, status: str | None = None) -> Iterator[FileRecord]:
        sql = "SELECT * FROM files"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        for row in self.conn.execute(sql + " ORDER BY id"):
            yield FileRecord.from_row(row)

    def set_status(self, file_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE files SET status = ?, last_error = ?, last_seen = datetime('now') WHERE id = ?",
            (status, error, file_id),
        )
        self.conn.commit()

    def mark_missing(self, paths: Iterable[str]) -> int:
        """Marca como 'missing' archivos que ya no estan en disco."""
        paths = list(paths)
        if not paths:
            return 0
        placeholders = ",".join("?" * len(paths))
        cur = self.conn.execute(
            f"UPDATE files SET status = 'missing' WHERE path IN ({placeholders})",
            paths,
        )
        self.conn.commit()
        return cur.rowcount

    def all_indexed_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM files")}

    # -- embeddings ----------------------------------------------------------

    def put_embedding(self, file_id: int, kind: str, model_id: str, vec: np.ndarray) -> None:
        self.conn.execute(
            "INSERT INTO embeddings (file_id, kind, model_id, dim, vector) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(file_id, kind, model_id) DO UPDATE SET "
            "  vector = excluded.vector, dim = excluded.dim, created_at = datetime('now')",
            (file_id, kind, model_id, int(np.size(vec)), pack_vector(vec)),
        )
        self.conn.commit()

    def get_embedding(self, file_id: int, kind: str, model_id: str) -> np.ndarray | None:
        row = self.conn.execute(
            "SELECT vector, dim FROM embeddings WHERE file_id = ? AND kind = ? AND model_id = ?",
            (file_id, kind, model_id),
        ).fetchone()
        return unpack_vector(row["vector"], row["dim"]) if row else None

    def load_matrix(self, kind: str, model_id: str) -> tuple[list[int], np.ndarray]:
        """Todos los embeddings de un (kind, model_id) como matriz (n, dim).

        Con vectores normalizados, `matriz @ consulta` da todas las similitudes
        coseno de golpe. A la escala de una coleccion personal (10^4-10^5) la
        fuerza bruta con NumPy tarda milisegundos y evita una dependencia de
        base vectorial. Ver docs/esquema-de-datos.md
        """
        rows = list(
            self.conn.execute(
                "SELECT file_id, vector, dim FROM embeddings WHERE kind = ? AND model_id = ? "
                "ORDER BY file_id",
                (kind, model_id),
            )
        )
        if not rows:
            return [], np.empty((0, 0), dtype=np.float32)
        dim = rows[0]["dim"]
        ids = [r["file_id"] for r in rows]
        matrix = np.vstack([unpack_vector(r["vector"], dim) for r in rows])
        return ids, matrix

    # -- vectores de carpeta -------------------------------------------------

    def put_folder_vector(
        self,
        folder_id: int,
        source: str,
        kind: str,
        model_id: str,
        vec: np.ndarray,
        n_samples: int = 0,
    ) -> None:
        """source: 'description' | 'centroid'"""
        self.conn.execute(
            "INSERT INTO folder_vectors (folder_id, source, kind, model_id, dim, vector, n_samples)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(folder_id, source, kind, model_id) DO UPDATE SET "
            "  vector = excluded.vector, dim = excluded.dim, "
            "  n_samples = excluded.n_samples, updated_at = datetime('now')",
            (folder_id, source, kind, model_id, int(np.size(vec)), pack_vector(vec), n_samples),
        )
        self.conn.commit()

    def get_folder_vector(
        self, folder_id: int, source: str, kind: str, model_id: str
    ) -> tuple[np.ndarray, int] | None:
        row = self.conn.execute(
            "SELECT vector, dim, n_samples FROM folder_vectors "
            "WHERE folder_id = ? AND source = ? AND kind = ? AND model_id = ?",
            (folder_id, source, kind, model_id),
        ).fetchone()
        if not row:
            return None
        return unpack_vector(row["vector"], row["dim"]), row["n_samples"]

    def update_centroid(self, folder_id: int, kind: str, model_id: str, vec: np.ndarray) -> None:
        """Anade un vector al centroide de forma incremental.

            c_nuevo = normalizar( (c_viejo * n + v) / (n + 1) )

        Incremental y no recalculado: confirmar un archivo no debe obligar a
        releer la carpeta entera.
        """
        current = self.get_folder_vector(folder_id, "centroid", kind, model_id)
        v = np.asarray(vec, dtype=np.float32).ravel()
        if current is None:
            new_vec, n = v, 1
        else:
            old, n = current
            new_vec = (old * n + v) / (n + 1)
            n += 1
        self.put_folder_vector(folder_id, "centroid", kind, model_id, new_vec, n)

    # -- ejemplares ----------------------------------------------------------

    def add_exemplar(
        self,
        folder_id: int,
        kind: str,
        model_id: str,
        vec: np.ndarray,
        source_file: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO exemplars (folder_id, kind, model_id, dim, vector, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (folder_id, kind, model_id, int(np.size(vec)), pack_vector(vec), source_file),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_exemplars(self, folder_id: int, kind: str, model_id: str) -> np.ndarray:
        rows = list(
            self.conn.execute(
                "SELECT vector, dim FROM exemplars "
                "WHERE folder_id = ? AND kind = ? AND model_id = ?",
                (folder_id, kind, model_id),
            )
        )
        if not rows:
            return np.empty((0, 0), dtype=np.float32)
        return np.vstack([unpack_vector(r["vector"], r["dim"]) for r in rows])

    # -- decisiones ----------------------------------------------------------

    def record_decision(
        self,
        file_id: int,
        candidates: list[dict[str, Any]],
        stage: str,
        model_id: str | None = None,
        profile: str | None = None,
    ) -> int:
        """Registra una propuesta con su top-N COMPLETO.

        Guardar todos los candidatos y no solo el elegido es lo que permite
        despues distinguir un fallo de calibracion (la correcta estaba en el
        puesto 2) de uno de representacion (no estaba en la lista).
        Ver docs/motor-de-decision.md
        """
        top = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        confidence = float(top["score"]) if top else 0.0
        margin = confidence - float(second["score"]) if second else confidence

        cur = self.conn.execute(
            "INSERT INTO decisions "
            "(file_id, candidates, proposed_folder_id, confidence, margin, stage, model_id, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                json.dumps(candidates, ensure_ascii=False),
                top["folder_id"] if top else None,
                confidence,
                margin,
                stage,
                model_id,
                profile,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def settle_decision(self, decision_id: int, verdict: str, final_folder_id: int | None) -> None:
        """verdict: 'accepted' | 'corrected' | 'rejected'"""
        self.conn.execute(
            "UPDATE decisions SET verdict = ?, final_folder_id = ?, decided_at = datetime('now') "
            "WHERE id = ?",
            (verdict, final_folder_id, decision_id),
        )
        self.conn.commit()

    def pending_decisions(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT d.*, f.path AS file_path, f.name AS file_name "
                "FROM decisions d JOIN files f ON f.id = d.file_id "
                "WHERE d.verdict = 'pending' ORDER BY d.confidence DESC"
            )
        )

    # -- journal -------------------------------------------------------------

    def plan_operation(
        self,
        op: str,
        src: str | None = None,
        dst: str | None = None,
        file_id: int | None = None,
        decision_id: int | None = None,
    ) -> int:
        """Registra la INTENCION antes de tocar el disco.

        Escribir antes y no despues es lo que hace util al journal: si el
        proceso muere a mitad de un movimiento, al arrancar quedan entradas en
        'planned' y se sabe exactamente que quedo a medias.
        """
        cur = self.conn.execute(
            "INSERT INTO journal (op, src, dst, file_id, decision_id, state) "
            "VALUES (?, ?, ?, ?, ?, 'planned')",
            (op, src, dst, file_id, decision_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def complete_operation(self, journal_id: int, error: str | None = None) -> None:
        state = "failed" if error else "done"
        self.conn.execute(
            "UPDATE journal SET state = ?, error = ?, done_at = datetime('now') WHERE id = ?",
            (state, error, journal_id),
        )
        self.conn.commit()

    def mark_undone(self, journal_id: int) -> None:
        self.conn.execute(
            "UPDATE journal SET state = 'undone', undone_at = datetime('now') WHERE id = ?",
            (journal_id,),
        )
        self.conn.commit()

    def undoable_operations(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM journal WHERE state = 'done' AND op IN ('move', 'rename') "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )

    def interrupted_operations(self) -> list[sqlite3.Row]:
        """Operaciones que quedaron en 'planned': el proceso murio a mitad."""
        return list(self.conn.execute("SELECT * FROM journal WHERE state = 'planned' ORDER BY id"))

    # -- estadisticas --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
            return self.conn.execute(sql, params).fetchone()[0]

        by_status = {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status")
        }
        return {
            "files": scalar("SELECT COUNT(*) FROM files"),
            "by_status": by_status,
            "folders": scalar("SELECT COUNT(*) FROM folders"),
            "watched_dirs": scalar("SELECT COUNT(*) FROM watched_dirs WHERE enabled = 1"),
            "embeddings": scalar("SELECT COUNT(*) FROM embeddings"),
            "exemplars": scalar("SELECT COUNT(*) FROM exemplars"),
            "pending_decisions": scalar("SELECT COUNT(*) FROM decisions WHERE verdict = 'pending'"),
            "moves": scalar("SELECT COUNT(*) FROM journal WHERE state = 'done'"),
        }
