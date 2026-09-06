"""Capa de indice: todo el acceso a SQLite pasa por aqui.

Ninguna otra parte del codigo escribe SQL. Eso mantiene en un solo sitio las dos
invariantes que importan:

  1. Un vector NUNCA se lee ni se compara sin filtrar por (model_id, vector_space).
     Vectores de modelos distintos no dan un resultado malo, dan un resultado
     sin sentido.
  2. Toda operacion sobre el sistema de archivos queda registrada en el journal
     ANTES de ejecutarse.

Ver docs/referencia/esquema-de-datos.md
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

SCHEMA_VERSION = 4  # extractor, polaridad de ejemplares, excepciones
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


def unpack_vector(blob: bytes, dimensions: int) -> np.ndarray:
    vec = np.frombuffer(blob, dtype="<f4")
    if vec.size != dimensions:
        raise ValueError(
            f"vector corrupto: se esperaban {dimensions} dimensiones, hay {vec.size}"
        )
    return vec


# ---------------------------------------------------------------------------
# Registros
# ---------------------------------------------------------------------------


@dataclass
class ItemRecord:
    """Un archivo o una carpeta tratada como unidad."""

    id: int
    item_type: str
    path: str
    name: str
    extension: str
    size_bytes: int
    fs_modified_at: float
    status: str
    content_hash: str | None = None
    folder_id: int | None = None
    child_count: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ItemRecord:
        return cls(
            id=row["id"],
            item_type=row["item_type"],
            path=row["path"],
            name=row["name"],
            extension=row["extension"],
            size_bytes=row["size_bytes"],
            fs_modified_at=row["fs_modified_at"],
            status=row["status"],
            content_hash=row["content_hash"],
            folder_id=row["folder_id"],
            child_count=row["child_count"],
        )


@dataclass
class FolderRecord:
    id: int
    path: str
    description: str
    status: str
    auto_move: bool
    is_trash: bool
    parent_id: int | None = None
    organize_by: str = "none"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> FolderRecord:
        return cls(
            id=row["id"],
            path=row["path"],
            description=row["description"],
            status=row["status"],
            auto_move=bool(row["auto_move"]),
            is_trash=bool(row["is_trash"]),
            parent_id=row["parent_id"],
            organize_by=row["organize_by"],
        )


# ---------------------------------------------------------------------------
# Indice
# ---------------------------------------------------------------------------


class Index:
    """Acceso al indice SQLite.

    Uso:
        with Index(path) as idx:
            idx.upsert_item(...)
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

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _migrate(self) -> None:
        """Aplica el esquema, comprobando la version ANTES de tocar nada.

        El orden importa. Con CREATE TABLE IF NOT EXISTS, una base creada por
        una version anterior conserva sus tablas viejas: el script no las
        recrea y el fallo aparece mucho despues, como un criptico
        'no such column'. Comprobando primero, el mensaje dice que pasa.

        Mientras el proyecto este en desarrollo temprano la via soportada es
        borrar el indice y reindexar: es una cache reconstruible, la
        informacion que importa esta en los archivos del usuario.
        """
        if self._table_exists("meta"):
            current = self.get_meta("schema_version")
            if current is not None and int(current) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"El indice de {self.db_path} es de la version {current} del esquema "
                    f"y este codigo espera la {SCHEMA_VERSION}.\n"
                    f"Borra {self.db_path.parent} y vuelve a indexar."
                )

        self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.set_meta("schema_version", str(SCHEMA_VERSION))
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

    def add_watched_dir(self, path: str | Path, subdir_policy: str = "unit") -> int:
        """subdir_policy: 'unit' (cada subcarpeta es una cosa), 'ignore' o
        'descend' (cada archivo de dentro por separado)."""
        p = str(Path(path).resolve())
        cur = self.conn.execute(
            "INSERT INTO watched_dirs (path, subdir_policy) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  status = 'active', subdir_policy = excluded.subdir_policy",
            (p, subdir_policy),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM watched_dirs WHERE path = ?", (p,)).fetchone()
        return row["id"]

    def list_watched_dirs(self, active_only: bool = True) -> list[sqlite3.Row]:
        table = "active_watched_dirs" if active_only else "watched_dirs"
        return list(self.conn.execute(f"SELECT * FROM {table} ORDER BY path"))

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
            "  status = 'active', "
            "  updated_at = datetime('now')",
            (p, description, int(is_trash)),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM folders WHERE path = ?", (p,)).fetchone()
        return row["id"]

    def list_folders(self, active_only: bool = True) -> list[FolderRecord]:
        table = "active_folders" if active_only else "folders"
        return [
            FolderRecord.from_row(r)
            for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY path")
        ]

    def list_trash_folders(self) -> list[FolderRecord]:
        """Puede haber varias. Si ninguna carpeta normal supera el umbral se
        puntua solo entre estas, sin umbral: alguien tiene que quedarse el
        archivo."""
        return [
            FolderRecord.from_row(r)
            for r in self.conn.execute("SELECT * FROM active_folders WHERE is_trash = 1")
        ]

    def remove_folder(self, folder_id: int) -> None:
        """Baja logica: nunca DELETE.

        Conserva centroide, ejemplares e historial de decisiones, de modo que
        volver a anadir la carpeta recupera todo lo aprendido. Las hijas se
        promocionan a raiz: quitar /imagenes de la configuracion no invalida
        /imagenes/gatos como destino. Esa promocion se hace aqui a mano porque,
        al no haber DELETE, el ON DELETE SET NULL no llega a dispararse.
        """
        self.conn.execute(
            "UPDATE folders SET parent_id = NULL, updated_at = datetime('now') "
            "WHERE parent_id = ?",
            (folder_id,),
        )
        self.conn.execute(
            "UPDATE folders SET status = 'deleted', updated_at = datetime('now') WHERE id = ?",
            (folder_id,),
        )
        self.conn.commit()

    # -- archivos ------------------------------------------------------------

    def upsert_item(self, path: str | Path, status: str = "pending") -> int:
        """Da de alta o actualiza un archivo a partir de su estado en disco.

        Si el archivo ya existia y cambio de tamano o mtime, vuelve a 'pending'
        para que se reanalice: su contenido ya no es el que se indexo.
        """
        p = Path(path)
        stat = p.stat()
        resolved = str(p.resolve())

        existing = self.get_item_by_path(resolved)
        if existing:
            changed = existing.size_bytes != stat.st_size or abs(existing.fs_modified_at - stat.st_mtime) > 1e-6
            new_status = "pending" if changed else existing.status
            self.conn.execute(
                "UPDATE items SET size_bytes = ?, fs_modified_at = ?, status = ?, last_seen_at = datetime('now') "
                "WHERE id = ?",
                (stat.st_size, stat.st_mtime, new_status, existing.id),
            )
            if changed:
                # El contenido cambio: los embeddings viejos ya no describen
                # este archivo.
                self.conn.execute("DELETE FROM embeddings WHERE item_id = ?", (existing.id,))
            self.conn.commit()
            return existing.id

        cur = self.conn.execute(
            "INSERT INTO items (path, name, extension, size_bytes, fs_modified_at, status) VALUES (?, ?, ?, ?, ?, ?)",
            (resolved, p.name, p.suffix.lower(), stat.st_size, stat.st_mtime, status),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_item_by_path(self, path: str | Path) -> ItemRecord | None:
        row = self.conn.execute(
            "SELECT * FROM items WHERE path = ?", (str(Path(path).resolve()),)
        ).fetchone()
        return ItemRecord.from_row(row) if row else None

    def get_item(self, item_id: int) -> ItemRecord | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return ItemRecord.from_row(row) if row else None

    def iter_items(self, status: str | None = None) -> Iterator[ItemRecord]:
        sql = "SELECT * FROM items"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        for row in self.conn.execute(sql + " ORDER BY id"):
            yield ItemRecord.from_row(row)

    def set_status(self, item_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE items SET status = ?, last_error = ?, last_seen_at = datetime('now') WHERE id = ?",
            (status, error, item_id),
        )
        self.conn.commit()

    def mark_missing(self, paths: Iterable[str]) -> int:
        """Marca como 'missing' archivos que ya no estan en disco."""
        paths = list(paths)
        if not paths:
            return 0
        placeholders = ",".join("?" * len(paths))
        cur = self.conn.execute(
            f"UPDATE items SET status = 'missing' WHERE path IN ({placeholders})",
            paths,
        )
        self.conn.commit()
        return cur.rowcount

    def all_indexed_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM items")}

    # -- embeddings ----------------------------------------------------------

    def put_embedding(
        self,
        item_id: int,
        vector_space: str,
        model_id: str,
        vec: np.ndarray,
        extractor: str = "",
    ) -> None:
        """extractor: con que receta se obtuvo el contenido ('pdf-text-v1'...).

        Un vector caduca por dos motivos, no uno: si cambia el modelo o si
        cambia la receta de extraccion. Guardarla permite detectar el segundo.
        """
        self.conn.execute(
            "INSERT INTO embeddings "
            "(item_id, vector_space, model_id, extractor, dimensions, vector) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id, vector_space, model_id) DO UPDATE SET "
            "  vector = excluded.vector, dimensions = excluded.dimensions, "
            "  extractor = excluded.extractor, created_at = datetime('now')",
            (item_id, vector_space, model_id, extractor, int(np.size(vec)), pack_vector(vec)),
        )
        self.conn.commit()

    def stale_embeddings(self, vector_space: str, model_id: str, extractor: str) -> list[int]:
        """Items cuyo vector se genero con otra receta y hay que regenerar."""
        return [
            r["item_id"]
            for r in self.conn.execute(
                "SELECT item_id FROM embeddings "
                "WHERE vector_space = ? AND model_id = ? AND extractor != ?",
                (vector_space, model_id, extractor),
            )
        ]

    def get_embedding(
        self, item_id: int, vector_space: str, model_id: str
    ) -> np.ndarray | None:
        row = self.conn.execute(
            "SELECT vector, dimensions FROM embeddings "
            "WHERE item_id = ? AND vector_space = ? AND model_id = ?",
            (item_id, vector_space, model_id),
        ).fetchone()
        return unpack_vector(row["vector"], row["dimensions"]) if row else None

    def load_matrix(self, vector_space: str, model_id: str) -> tuple[list[int], np.ndarray]:
        """Todos los embeddings de un (vector_space, model_id) como matriz.

        Con vectores normalizados, `matriz @ consulta` da todas las similitudes
        coseno de golpe. A la escala de una coleccion personal (10^4-10^5) la
        fuerza bruta con NumPy tarda milisegundos y evita una dependencia de
        base vectorial. Ver docs/referencia/esquema-de-datos.md
        """
        rows = list(
            self.conn.execute(
                "SELECT item_id, vector, dimensions FROM embeddings "
                "WHERE vector_space = ? AND model_id = ? ORDER BY item_id",
                (vector_space, model_id),
            )
        )
        if not rows:
            return [], np.empty((0, 0), dtype=np.float32)
        dimensions = rows[0]["dimensions"]
        ids = [r["item_id"] for r in rows]
        matrix = np.vstack([unpack_vector(r["vector"], dimensions) for r in rows])
        return ids, matrix

    # -- vectores de carpeta -------------------------------------------------

    def put_folder_vector(
        self,
        folder_id: int,
        signal: str,
        vector_space: str,
        model_id: str,
        vec: np.ndarray,
        sample_count: int = 0,
    ) -> None:
        """signal: 'description' | 'centroid' -- las dos senales del scoring."""
        self.conn.execute(
            "INSERT INTO folder_vectors "
            "(folder_id, signal, vector_space, model_id, dimensions, vector, sample_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(folder_id, signal, vector_space, model_id) DO UPDATE SET "
            "  vector = excluded.vector, dimensions = excluded.dimensions, "
            "  sample_count = excluded.sample_count, updated_at = datetime('now')",
            (
                folder_id,
                signal,
                vector_space,
                model_id,
                int(np.size(vec)),
                pack_vector(vec),
                sample_count,
            ),
        )
        self.conn.commit()

    def get_folder_vector(
        self, folder_id: int, signal: str, vector_space: str, model_id: str
    ) -> tuple[np.ndarray, int] | None:
        row = self.conn.execute(
            "SELECT vector, dimensions, sample_count FROM folder_vectors "
            "WHERE folder_id = ? AND signal = ? AND vector_space = ? AND model_id = ?",
            (folder_id, signal, vector_space, model_id),
        ).fetchone()
        if not row:
            return None
        return unpack_vector(row["vector"], row["dimensions"]), row["sample_count"]

    def update_centroid(
        self, folder_id: int, vector_space: str, model_id: str, vec: np.ndarray
    ) -> None:
        """Anade un vector al centroide de forma incremental.

            c_nuevo = normalizar( (c_viejo * n + v) / (n + 1) )

        Incremental y no recalculado: confirmar un archivo no debe obligar a
        releer la carpeta entera.
        """
        current = self.get_folder_vector(folder_id, "centroid", vector_space, model_id)
        v = np.asarray(vec, dtype=np.float32).ravel()
        if current is None:
            new_vec, n = v, 1
        else:
            old, n = current
            new_vec = (old * n + v) / (n + 1)
            n += 1
        self.put_folder_vector(folder_id, "centroid", vector_space, model_id, new_vec, n)

    # -- ejemplares ----------------------------------------------------------

    def add_exemplar(
        self,
        folder_id: int,
        vector_space: str,
        model_id: str,
        vec: np.ndarray,
        source_path: str | None = None,
        polarity: str = "positive",
    ) -> int:
        """polarity: 'positive' (va aqui) o 'negative' (NO iba aqui).

        Los negativos se guardan pero no entran en el scoring de v1: un
        negativo puede ser una excepcion disfrazada de regla. Se usan para
        calibrar el umbral de la carpeta. Ver docs/diseno/motor-de-decision.md
        """
        cur = self.conn.execute(
            "INSERT INTO exemplars "
            "(folder_id, polarity, vector_space, model_id, dimensions, vector, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                folder_id,
                polarity,
                vector_space,
                model_id,
                int(np.size(vec)),
                pack_vector(vec),
                source_path,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_exemplars(
        self,
        folder_id: int,
        vector_space: str,
        model_id: str,
        polarity: str = "positive",
    ) -> np.ndarray:
        """Por defecto solo los positivos: son los unicos que entran en el
        scoring de v1."""
        rows = list(
            self.conn.execute(
                "SELECT vector, dimensions FROM exemplars "
                "WHERE folder_id = ? AND vector_space = ? AND model_id = ? AND polarity = ?",
                (folder_id, vector_space, model_id, polarity),
            )
        )
        if not rows:
            return np.empty((0, 0), dtype=np.float32)
        return np.vstack([unpack_vector(r["vector"], r["dimensions"]) for r in rows])

    # -- decisiones ----------------------------------------------------------

    def record_decision(
        self,
        item_id: int,
        candidates: list[dict[str, Any]],
        decided_by: str,
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
            "(item_id, candidates_json, proposed_folder_id, confidence, margin, decided_by, model_id, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                json.dumps(candidates, ensure_ascii=False),
                top["folder_id"] if top else None,
                confidence,
                margin,
                decided_by,
                model_id,
                profile,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def settle_decision(
        self,
        decision_id: int,
        verdict: str,
        final_folder_id: int | None,
        is_exception: bool = False,
    ) -> None:
        """verdict: 'accepted' | 'corrected' | 'rejected'

        is_exception distingue "te equivocaste" de "esta vez quiero otra cosa".
        Una excepcion no genera ejemplar y no cuenta como desacuerdo: mover una
        factura a /impuestos porque toca la declaracion no significa que
        /facturas estuviera mal.
        """
        self.conn.execute(
            "UPDATE decisions SET verdict = ?, final_folder_id = ?, is_exception = ?, "
            "  decided_at = datetime('now') WHERE id = ?",
            (verdict, final_folder_id, int(is_exception), decision_id),
        )
        self.conn.commit()

    def agreement_rate(self) -> dict[str, Any]:
        """Tasa de acuerdo con el usuario, no "precision".

        No existe una verdad objetiva: el objetivo es colocar los archivos
        donde ESTE usuario los quiere. Las excepciones se excluyen porque no
        son desacuerdos con la propuesta.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "  SUM(verdict = 'accepted') AS aceptadas "
            "FROM decisions WHERE verdict != 'pending' AND is_exception = 0"
        ).fetchone()
        total = row["total"] or 0
        aceptadas = row["aceptadas"] or 0
        return {
            "decididas": total,
            "aceptadas": aceptadas,
            "tasa_acuerdo": (aceptadas / total) if total else None,
        }

    def pending_decisions(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT d.*, i.path AS item_path, i.name AS item_name "
                "FROM decisions d JOIN items i ON i.id = d.item_id "
                "WHERE d.verdict = 'pending' ORDER BY d.confidence DESC"
            )
        )

    # -- journal -------------------------------------------------------------

    def plan_operation(
        self,
        operation: str,
        source_path: str | None = None,
        dest_path: str | None = None,
        item_id: int | None = None,
        decision_id: int | None = None,
    ) -> int:
        """Registra la INTENCION antes de tocar el disco.

        Escribir antes y no despues es lo que hace util al journal: si el
        proceso muere a mitad de un movimiento, al arrancar quedan entradas en
        'planned' y se sabe exactamente que quedo a medias.
        """
        cur = self.conn.execute(
            "INSERT INTO journal (operation, source_path, dest_path, item_id, decision_id, state) "
            "VALUES (?, ?, ?, ?, ?, 'planned')",
            (operation, source_path, dest_path, item_id, decision_id),
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
                "SELECT * FROM journal WHERE state = 'done' AND operation IN ('move', 'rename') "
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
            for r in self.conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        }
        return {
            "items": scalar("SELECT COUNT(*) FROM items"),
            "by_status": by_status,
            "folders": scalar("SELECT COUNT(*) FROM active_folders"),
            "watched_dirs": scalar("SELECT COUNT(*) FROM active_watched_dirs"),
            "embeddings": scalar("SELECT COUNT(*) FROM embeddings"),
            "exemplars": scalar("SELECT COUNT(*) FROM exemplars"),
            "pending_decisions": scalar("SELECT COUNT(*) FROM decisions WHERE verdict = 'pending'"),
            "moves": scalar("SELECT COUNT(*) FROM journal WHERE state = 'done'"),
        }
