"""Mide si SQLite + NumPy bastan, o si hace falta una base vectorial.

Respalda con numeros la decision documentada en docs/referencia/esquema-de-datos.md. No es
un test (pytest no lo recoge: no empieza por test_), es una medicion que se
ejecuta a mano:

    python tests/bench_vectors.py

Conviene reejecutarlo en las dos maquinas de desarrollo: la comparacion entre
ambas es parte del metodo de trabajo (ver docs/proyecto/entorno-de-desarrollo.md).

Mide los dos casos que ocurren de verdad:
  [A] clasificar un archivo  -> comparar contra las CARPETAS (decenas)
  [B] busqueda semantica v2  -> comparar contra TODOS los archivos
"""

from __future__ import annotations

import os
import platform
import tempfile
import time
from pathlib import Path

import numpy as np

from fileflow.db.index import Index, pack_vector

DIM = 384  # multilingual-e5-small, perfil ligero
N_FILES = 100_000  # coleccion personal muy grande
N_FOLDERS = 50  # carpetas destino, el caso realista

rng = np.random.default_rng(0)


def random_unit(n: int, dim: int) -> np.ndarray:
    """Vectores aleatorios normalizados, como los que produce un embedder."""
    v = rng.standard_normal((n, dim)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def main() -> None:
    print(f"Maquina : {platform.node()}  ({platform.processor()})")
    print(f"Vectores: {DIM} dimensiones -> {DIM * 4} bytes cada uno\n")

    work = Path(tempfile.mkdtemp(prefix="fileflow_bench_"))
    db_path = work / "bench.db"
    vectors = random_unit(N_FILES, DIM)

    # -- escritura ---------------------------------------------------------
    print(f"[1] Escribiendo {N_FILES:,} embeddings en SQLite...")
    index = Index(db_path)
    rows = [(i, "text", "bench", DIM, pack_vector(vectors[i])) for i in range(N_FILES)]

    t0 = time.perf_counter()
    # Insercion directa: aqui se mide el almacenamiento, no la API del indice.
    index.conn.execute("PRAGMA foreign_keys = OFF")
    index.conn.executemany(
        "INSERT INTO embeddings (item_id, vector_space, model_id, dimensions, vector) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    index.conn.commit()
    write_s = time.perf_counter() - t0

    print(f"    {write_s:.2f} s  ({N_FILES / write_s:,.0f} vectores/s)")
    print(f"    Disco: {os.path.getsize(db_path) / 1024**2:.1f} MB\n")

    # -- lectura -----------------------------------------------------------
    print(f"[2] Cargando los {N_FILES:,} vectores a memoria...")
    t0 = time.perf_counter()
    _, matrix = index.load_matrix("text", "bench")
    load_s = time.perf_counter() - t0
    print(f"    {load_s:.2f} s  matriz {matrix.shape}  RAM {matrix.nbytes / 1024**2:.1f} MB\n")

    query = random_unit(1, DIM)[0]

    # -- caso A: el que ocurre constantemente ------------------------------
    print(f"[A] Clasificar 1 archivo contra {N_FOLDERS} carpetas")
    folders = random_unit(N_FOLDERS, DIM)
    times = []
    for _ in range(1000):
        t0 = time.perf_counter()
        int(np.argmax(folders @ query))
        times.append((time.perf_counter() - t0) * 1e6)
    median_us = float(np.median(times))
    print(f"    {median_us:.1f} microsegundos  ->  {1e6 / median_us:,.0f} archivos/s")
    print("    (generar el embedding costara 50-200 ms: la busqueda es ~10.000x mas barata)\n")

    # -- caso B: busqueda semantica de v2 ----------------------------------
    print(f"[B] Busqueda semantica sobre los {N_FILES:,} archivos (fuerza bruta)")
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        scores = matrix @ query
        np.argpartition(-scores, 10)[:10]
        times.append((time.perf_counter() - t0) * 1000)
    median_ms = float(np.median(times))
    print(f"    {median_ms:.1f} ms  (un indice ANN daria ~1 ms, aproximado y con dependencia)\n")

    # -- extrapolacion -----------------------------------------------------
    print("[3] Por tamano de coleccion (la RAM es el limite real, no el tiempo):")
    print(f"    {'archivos':>10}  {'disco':>10}  {'RAM':>10}  {'busqueda':>10}")
    for n in (5_000, 20_000, 100_000, 500_000):
        mb = n * DIM * 4 / 1024**2
        ms = median_ms * n / N_FILES
        print(f"    {n:>10,}  {mb:>7.1f} MB  {mb:>7.1f} MB  {ms:>8.1f} ms")

    index.close()


if __name__ == "__main__":
    main()
