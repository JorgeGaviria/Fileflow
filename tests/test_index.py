"""Pruebas de la capa de indice.

El foco esta en las invariantes que pueden hacer perder archivos o corromper
comparaciones, no en el CRUD trivial.
"""

from __future__ import annotations

import numpy as np
import pytest

from fileflow.db.index import Index, pack_vector, unpack_vector


@pytest.fixture
def index(tmp_path):
    with Index(tmp_path / "test.db") as idx:
        yield idx


@pytest.fixture
def sample_file(tmp_path):
    path = tmp_path / "factura_enero.pdf"
    path.write_bytes(b"contenido de prueba")
    return path


# -- vectores ---------------------------------------------------------------


def test_pack_normaliza_a_norma_1():
    """Se normaliza al guardar para que el coseno sea un producto escalar."""
    vec = np.array([3.0, 4.0], dtype=np.float32)
    restored = unpack_vector(pack_vector(vec), 2)
    assert np.isclose(np.linalg.norm(restored), 1.0)
    assert np.allclose(restored, [0.6, 0.8])


def test_pack_soporta_vector_cero():
    """Un vector nulo no debe provocar division por cero."""
    restored = unpack_vector(pack_vector(np.zeros(4, dtype=np.float32)), 4)
    assert np.allclose(restored, 0.0)


def test_unpack_rechaza_dimension_incorrecta():
    """Mejor fallar ruidosamente que comparar vectores incompatibles."""
    blob = pack_vector(np.ones(384, dtype=np.float32))
    with pytest.raises(ValueError, match="vector corrupto"):
        unpack_vector(blob, 768)


# -- archivos ---------------------------------------------------------------


def test_upsert_es_idempotente(index, sample_file):
    first = index.upsert_file(sample_file)
    second = index.upsert_file(sample_file)
    assert first == second
    assert index.stats()["files"] == 1


def test_archivo_modificado_vuelve_a_pending(index, sample_file):
    """Si el contenido cambia, el analisis previo ya no vale."""
    file_id = index.upsert_file(sample_file)
    index.set_status(file_id, "analyzed")
    index.put_embedding(file_id, "text", "e5-small", np.ones(384, dtype=np.float32))

    sample_file.write_bytes(b"contenido distinto y mas largo que antes")
    index.upsert_file(sample_file)

    assert index.get_file(file_id).status == "pending"
    # Los embeddings viejos describian otro contenido: deben desaparecer.
    assert index.get_embedding(file_id, "text", "e5-small") is None


# -- aislamiento entre modelos ----------------------------------------------


def test_modelos_distintos_conviven_sin_mezclarse(index, sample_file):
    """La invariante central: los vectores se separan por model_id.

    Permite cambiar de perfil de forma incremental en vez de borrarlo todo.
    """
    file_id = index.upsert_file(sample_file)
    index.put_embedding(file_id, "text", "e5-small", np.ones(384, dtype=np.float32))
    index.put_embedding(file_id, "text", "bge-m3", np.ones(1024, dtype=np.float32))

    assert index.get_embedding(file_id, "text", "e5-small").shape == (384,)
    assert index.get_embedding(file_id, "text", "bge-m3").shape == (1024,)

    ids, matrix = index.load_matrix("text", "e5-small")
    assert matrix.shape == (1, 384)  # solo el del modelo pedido


def test_texto_e_imagen_no_se_mezclan(index, sample_file):
    file_id = index.upsert_file(sample_file)
    index.put_embedding(file_id, "text", "m", np.ones(384, dtype=np.float32))
    index.put_embedding(file_id, "image", "m", np.ones(512, dtype=np.float32))

    _, text_matrix = index.load_matrix("text", "m")
    _, image_matrix = index.load_matrix("image", "m")
    assert text_matrix.shape == (1, 384)
    assert image_matrix.shape == (1, 512)


# -- centroides -------------------------------------------------------------


def test_centroide_incremental(index, tmp_path):
    """El centroide se actualiza sin releer la carpeta entera."""
    folder_id = index.add_folder(tmp_path / "gatos", "fotos de mis gatos")

    index.update_centroid(folder_id, "image", "clip", np.array([1.0, 0.0], dtype=np.float32))
    _, n = index.get_folder_vector(folder_id, "centroid", "image", "clip")
    assert n == 1

    index.update_centroid(folder_id, "image", "clip", np.array([0.0, 1.0], dtype=np.float32))
    vec, n = index.get_folder_vector(folder_id, "centroid", "image", "clip")
    assert n == 2
    # La media de dos vectores ortogonales queda a 45 grados de ambos.
    assert np.allclose(vec, [0.7071, 0.7071], atol=1e-3)


def test_descripcion_y_centroide_son_independientes(index, tmp_path):
    """Las dos senales del scoring conviven sin pisarse."""
    folder_id = index.add_folder(tmp_path / "docs", "documentos")
    index.put_folder_vector(folder_id, "description", "text", "m", np.array([1.0, 0.0]))
    index.put_folder_vector(folder_id, "centroid", "text", "m", np.array([0.0, 1.0]), n_samples=5)

    desc, _ = index.get_folder_vector(folder_id, "description", "text", "m")
    cent, n = index.get_folder_vector(folder_id, "centroid", "text", "m")
    assert np.allclose(desc, [1.0, 0.0])
    assert np.allclose(cent, [0.0, 1.0])
    assert n == 5


# -- decisiones -------------------------------------------------------------


def test_decision_guarda_todos_los_candidatos(index, sample_file, tmp_path):
    """Guardar solo el elegido impediria distinguir un fallo de calibracion
    de uno de representacion."""
    file_id = index.upsert_file(sample_file)
    a = index.add_folder(tmp_path / "facturas", "facturas")
    b = index.add_folder(tmp_path / "contratos", "contratos")

    decision_id = index.record_decision(
        file_id,
        [{"folder_id": a, "score": 0.82}, {"folder_id": b, "score": 0.61}],
        stage="semantic",
        model_id="e5-small",
    )

    row = index.pending_decisions()[0]
    assert row["id"] == decision_id
    assert row["proposed_folder_id"] == a
    assert np.isclose(row["confidence"], 0.82)
    assert np.isclose(row["margin"], 0.21)  # el margen decide si es ambigua


def test_correccion_del_usuario(index, sample_file, tmp_path):
    file_id = index.upsert_file(sample_file)
    a = index.add_folder(tmp_path / "a", "a")
    b = index.add_folder(tmp_path / "b", "b")
    decision_id = index.record_decision(file_id, [{"folder_id": a, "score": 0.9}], stage="semantic")

    index.settle_decision(decision_id, "corrected", b)

    assert index.pending_decisions() == []
    row = index.conn.execute(
        "SELECT verdict, final_folder_id FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row["verdict"] == "corrected"
    assert row["final_folder_id"] == b


# -- journal ----------------------------------------------------------------


def test_journal_registra_la_intencion_antes_de_actuar(index, sample_file):
    """La entrada existe en 'planned' antes de tocar el disco: si el proceso
    muere a mitad, se sabe que quedo pendiente."""
    file_id = index.upsert_file(sample_file)
    journal_id = index.plan_operation("move", src="/a/x.pdf", dst="/b/x.pdf", file_id=file_id)

    assert [r["id"] for r in index.interrupted_operations()] == [journal_id]

    index.complete_operation(journal_id)
    assert index.interrupted_operations() == []
    assert [r["id"] for r in index.undoable_operations()] == [journal_id]


def test_operacion_deshecha_sale_de_la_lista(index):
    journal_id = index.plan_operation("move", src="/a/x", dst="/b/x")
    index.complete_operation(journal_id)
    index.mark_undone(journal_id)
    assert index.undoable_operations() == []


def test_operacion_fallida_no_es_deshacible(index):
    """Lo que nunca se ejecuto no hay que deshacerlo."""
    journal_id = index.plan_operation("move", src="/a/x", dst="/b/x")
    index.complete_operation(journal_id, error="archivo bloqueado")
    assert index.undoable_operations() == []
