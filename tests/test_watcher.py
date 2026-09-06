"""Pruebas del watcher: filtros, estabilidad y reconciliacion."""

from __future__ import annotations

from pathlib import Path

import pytest

from fileflow.db.index import Index
from fileflow.watcher.reconcile import reconcile, scan_directory
from fileflow.watcher.stability import is_hidden, is_stable, is_temp_file, should_process


@pytest.fixture
def index(tmp_path):
    with Index(tmp_path / "test.db") as idx:
        yield idx


# -- filtros ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "pelicula.mkv.crdownload",  # Chrome
        "instalador.exe.part",  # Firefox
        "algo.tmp",
        "~$documento.docx",  # bloqueo de Office
        "backup.bak",
    ],
)
def test_descarta_temporales(tmp_path, name):
    assert is_temp_file(tmp_path / name)


@pytest.mark.parametrize(
    "name",
    ["factura.pdf", "foto.jpg", "notas.md", "archivo con espacios.txt", ".gitignore"],
)
def test_acepta_archivos_normales(tmp_path, name):
    assert not is_temp_file(tmp_path / name)


@pytest.mark.parametrize("name", [".gitignore", ".env"])
def test_los_dotfiles_son_ocultos_no_temporales(tmp_path, name):
    """Son configuracion, no contenido que reorganizar: se descartan por
    ocultos, que es la categoria honesta."""
    path = tmp_path / name
    path.write_bytes(b"contenido")
    assert not is_temp_file(path)
    assert is_hidden(path)
    assert not should_process(path)


def test_descarta_archivos_vacios(tmp_path):
    """Un archivo de 0 bytes suele ser un marcador recien creado, no contenido."""
    empty = tmp_path / "vacio.txt"
    empty.touch()
    assert not should_process(empty)


# -- estabilidad ------------------------------------------------------------


def test_archivo_quieto_es_estable(tmp_path):
    path = tmp_path / "listo.pdf"
    path.write_bytes(b"x" * 1000)
    assert is_stable(path, checks=2, interval=0.05)


def test_archivo_temporal_nunca_es_estable(tmp_path):
    path = tmp_path / "descarga.crdownload"
    path.write_bytes(b"x" * 1000)
    assert not is_stable(path, checks=2, interval=0.05)


def test_archivo_inexistente_no_es_estable(tmp_path):
    assert not is_stable(tmp_path / "no_existe.pdf", checks=2, interval=0.05)


def test_archivo_que_crece_no_es_estable(tmp_path):
    """Simula una descarga en curso: el tamano cambia entre muestras."""
    path = tmp_path / "creciendo.bin"
    path.write_bytes(b"x" * 100)

    import threading

    def grow():
        import time

        time.sleep(0.05)
        with open(path, "ab") as fh:
            fh.write(b"y" * 5000)

    thread = threading.Thread(target=grow)
    thread.start()
    result = is_stable(path, checks=3, interval=0.1)
    thread.join()

    assert not result


# -- reconciliacion ---------------------------------------------------------


def test_scan_ignora_temporales(tmp_path):
    (tmp_path / "bueno.pdf").write_bytes(b"contenido")
    (tmp_path / "malo.crdownload").write_bytes(b"a medias")

    found = scan_directory(tmp_path)
    assert [p.name for p in found] == ["bueno.pdf"]


def test_reconcile_detecta_archivos_nuevos(tmp_path, index):
    """Lo que llego mientras la aplicacion estaba cerrada."""
    watched = tmp_path / "downloads"
    watched.mkdir()
    (watched / "a.pdf").write_bytes(b"uno")
    (watched / "b.jpg").write_bytes(b"dos")

    report = reconcile(index, [watched])

    assert len(report.new) == 2
    assert index.stats()["items"] == 2


def test_reconcile_detecta_modificados_y_desaparecidos(tmp_path, index):
    watched = tmp_path / "downloads"
    watched.mkdir()
    stable = watched / "estable.pdf"
    changing = watched / "cambia.txt"
    doomed = watched / "se_borra.tmp.pdf"
    for path, data in ((stable, b"uno"), (changing, b"dos"), (doomed, b"tres")):
        path.write_bytes(data)

    reconcile(index, [watched])

    changing.write_bytes(b"contenido nuevo y bastante mas largo")
    doomed.unlink()

    report = reconcile(index, [watched])

    assert [p.name for p in report.modified] == ["cambia.txt"]
    assert [Path(p).name for p in report.missing] == ["se_borra.tmp.pdf"]
    assert report.unchanged == 1
    assert index.get_item_by_path(stable).status == "pending"


def test_reconcile_es_idempotente(tmp_path, index):
    watched = tmp_path / "downloads"
    watched.mkdir()
    (watched / "a.pdf").write_bytes(b"contenido")

    reconcile(index, [watched])
    second = reconcile(index, [watched])

    assert second.new == []
    assert second.unchanged == 1
    assert index.stats()["items"] == 1
