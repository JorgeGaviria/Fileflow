"""Pruebas de la capa de indice.

El foco esta en las invariantes que pueden hacer perder archivos o corromper
comparaciones, no en el CRUD trivial.
"""

from __future__ import annotations

import sqlite3

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
    first = index.upsert_item(sample_file)
    second = index.upsert_item(sample_file)
    assert first == second
    assert index.stats()["items"] == 1


def test_archivo_modificado_vuelve_a_pending(index, sample_file):
    """Si el contenido cambia, el analisis previo ya no vale."""
    item_id = index.upsert_item(sample_file)
    index.set_status(item_id, "proposed")
    index.put_embedding(item_id, "text", "e5-small", np.ones(384, dtype=np.float32))

    sample_file.write_bytes(b"contenido distinto y mas largo que antes")
    index.upsert_item(sample_file)

    assert index.get_item(item_id).status == "pending"
    # Los embeddings viejos describian otro contenido: deben desaparecer.
    assert index.get_embedding(item_id, "text", "e5-small") is None


# -- aislamiento entre modelos ----------------------------------------------


def test_modelos_distintos_conviven_sin_mezclarse(index, sample_file):
    """La invariante central: los vectores se separan por model_id.

    Permite cambiar de perfil de forma incremental en vez de borrarlo todo.
    """
    item_id = index.upsert_item(sample_file)
    index.put_embedding(item_id, "text", "e5-small", np.ones(384, dtype=np.float32))
    index.put_embedding(item_id, "text", "bge-m3", np.ones(1024, dtype=np.float32))

    assert index.get_embedding(item_id, "text", "e5-small").shape == (384,)
    assert index.get_embedding(item_id, "text", "bge-m3").shape == (1024,)

    ids, matrix = index.load_matrix("text", "e5-small")
    assert matrix.shape == (1, 384)  # solo el del modelo pedido


def test_texto_e_imagen_no_se_mezclan(index, sample_file):
    item_id = index.upsert_item(sample_file)
    index.put_embedding(item_id, "text", "m", np.ones(384, dtype=np.float32))
    index.put_embedding(item_id, "image", "m", np.ones(512, dtype=np.float32))

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
    index.put_folder_vector(folder_id, "centroid", "text", "m", np.array([0.0, 1.0]), sample_count=5)

    desc, _ = index.get_folder_vector(folder_id, "description", "text", "m")
    cent, n = index.get_folder_vector(folder_id, "centroid", "text", "m")
    assert np.allclose(desc, [1.0, 0.0])
    assert np.allclose(cent, [0.0, 1.0])
    assert n == 5


# -- decisiones -------------------------------------------------------------


def test_decision_guarda_todos_los_candidatos(index, sample_file, tmp_path):
    """Guardar solo el elegido impediria distinguir un fallo de calibracion
    de uno de representacion."""
    item_id = index.upsert_item(sample_file)
    a = index.add_folder(tmp_path / "facturas", "facturas")
    b = index.add_folder(tmp_path / "contratos", "contratos")

    decision_id = index.record_decision(
        item_id,
        [{"folder_id": a, "score": 0.82}, {"folder_id": b, "score": 0.61}],
        decided_by="semantic",
        model_id="e5-small",
    )

    row = index.pending_decisions()[0]
    assert row["id"] == decision_id
    assert row["proposed_folder_id"] == a
    assert np.isclose(row["confidence"], 0.82)
    assert np.isclose(row["margin"], 0.21)  # el margen decide si es ambigua


def test_correccion_del_usuario(index, sample_file, tmp_path):
    item_id = index.upsert_item(sample_file)
    a = index.add_folder(tmp_path / "a", "a")
    b = index.add_folder(tmp_path / "b", "b")
    decision_id = index.record_decision(item_id, [{"folder_id": a, "score": 0.9}], decided_by="semantic")

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
    item_id = index.upsert_item(sample_file)
    journal_id = index.plan_operation("move", source_path="/a/x.pdf", dest_path="/b/x.pdf", item_id=item_id)

    assert [r["id"] for r in index.interrupted_operations()] == [journal_id]

    index.complete_operation(journal_id)
    assert index.interrupted_operations() == []
    assert [r["id"] for r in index.undoable_operations()] == [journal_id]


def test_operacion_deshecha_sale_de_la_lista(index):
    journal_id = index.plan_operation("move", source_path="/a/x", dest_path="/b/x")
    index.complete_operation(journal_id)
    index.mark_undone(journal_id)
    assert index.undoable_operations() == []


def test_operacion_fallida_no_es_deshacible(index):
    """Lo que nunca se ejecuto no hay que deshacerlo."""
    journal_id = index.plan_operation("move", source_path="/a/x", dest_path="/b/x")
    index.complete_operation(journal_id, error="archivo bloqueado")
    assert index.undoable_operations() == []


def test_estados_invalidos_se_rechazan(index, sample_file):
    """Los seis estados son un contrato cerrado. Un typo debe fallar en el
    momento, no escribir basura que rompa mucho despues y lejos."""
    item_id = index.upsert_item(sample_file)
    for valido in ("pending", "proposed", "moved", "ignored", "missing", "error"):
        index.set_status(item_id, valido)

    for invalido in ("analyzed", "unstable", "Pending", ""):
        with pytest.raises(sqlite3.IntegrityError):
            index.set_status(item_id, invalido)


def test_politica_de_subcarpetas(index, tmp_path):
    """subdirs sustituye al booleano 'recursive'. El defecto es 'unit' porque
    'descend' desperdiga carpetas que el usuario mantiene juntas."""
    d = tmp_path / "descargas"
    d.mkdir()
    index.add_watched_dir(d)
    assert index.list_watched_dirs()[0]["subdir_policy"] == "unit"

    index.add_watched_dir(d, subdir_policy="descend")
    assert index.list_watched_dirs()[0]["subdir_policy"] == "descend"

    with pytest.raises(sqlite3.IntegrityError):
        index.add_watched_dir(tmp_path / "otra", subdir_policy="recursivo")


def test_indice_de_otra_version_falla_con_mensaje_claro(tmp_path):
    """El fallo debe salir al ABRIR, diciendo que pasa.

    Con CREATE TABLE IF NOT EXISTS, una base vieja conserva sus tablas y sin
    esta comprobacion el error aparece mucho despues como un 'no such column'
    que no explica nada.
    """
    from fileflow.db import index as mod

    db = tmp_path / "viejo.db"
    with Index(db) as idx:
        idx.set_meta("schema_version", "1")

    with pytest.raises(RuntimeError, match="version 1 del esquema"):
        Index(db)


# -- borrado logico ----------------------------------------------------------


def test_quitar_carpeta_conserva_lo_aprendido(index, tmp_path):
    """El aprendizaje es la parte cara: quitar una carpeta y volver a anadirla
    debe recuperar centroide, ejemplares e historial intactos."""
    folder_id = index.add_folder(tmp_path / "documentos", "facturas y contratos")
    index.update_centroid(folder_id, "text", "e5-small", np.array([1.0, 0.0], dtype=np.float32))
    index.add_exemplar(folder_id, "text", "e5-small", np.array([0.0, 1.0], dtype=np.float32))

    index.remove_folder(folder_id)
    assert index.list_folders() == []                      # ya no compite
    assert len(index.list_folders(active_only=False)) == 1  # pero sigue ahi

    # Volver a anadirla reactiva la MISMA fila.
    otra_vez = index.add_folder(tmp_path / "documentos", "facturas y contratos")
    assert otra_vez == folder_id

    _, n = index.get_folder_vector(folder_id, "centroid", "text", "e5-small")
    assert n == 1
    assert index.get_exemplars(folder_id, "text", "e5-small").shape[0] == 1


def test_quitar_la_madre_promociona_a_las_hijas(index, tmp_path):
    """Quitar /imagenes de la configuracion no invalida /imagenes/gatos."""
    madre = index.add_folder(tmp_path / "imagenes", "todas las imagenes")
    hija = index.add_folder(tmp_path / "imagenes" / "gatos", "fotos de mis gatos")
    index.conn.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (madre, hija))
    index.conn.commit()

    index.remove_folder(madre)

    activas = index.list_folders()
    assert [f.id for f in activas] == [hija]
    assert activas[0].parent_id is None  # promocionada a raiz


def test_los_ids_no_se_reutilizan(index, tmp_path):
    """Con borrado duro, SQLite reasigna el id y la carpeta nueva heredaria
    los vectores de la vieja en silencio."""
    primera = index.add_folder(tmp_path / "documentos", "documentos")
    index.remove_folder(primera)
    segunda = index.add_folder(tmp_path / "musica", "musica")
    assert segunda != primera


# -- receta de extraccion, polaridad y excepciones ---------------------------


def test_vector_caduca_tambien_si_cambia_el_extractor(index, sample_file):
    """Un vector no solo caduca al cambiar el modelo. Si el extractor de PDF
    aprende OCR, los vectores de PDF escaneados son basura con el mismo
    modelo."""
    item_id = index.upsert_item(sample_file)
    index.put_embedding(item_id, "text", "e5-small", np.ones(384, dtype=np.float32),
                        extractor="pdf-text-v1")

    assert index.stale_embeddings("text", "e5-small", "pdf-text-v1") == []
    assert index.stale_embeddings("text", "e5-small", "pdf-ocr-v2") == [item_id]


def test_los_negativos_no_entran_en_el_scoring(index, tmp_path):
    """Se guardan para calibrar, pero get_exemplars solo devuelve positivos."""
    folder_id = index.add_folder(tmp_path / "facturas", "facturas")
    index.add_exemplar(folder_id, "text", "m", np.array([1.0, 0.0]), source_path="/a.pdf")
    index.add_exemplar(folder_id, "text", "m", np.array([0.0, 1.0]), source_path="/b.pdf",
                       polarity="negative")

    assert index.get_exemplars(folder_id, "text", "m").shape[0] == 1
    assert index.get_exemplars(folder_id, "text", "m", polarity="negative").shape[0] == 1


def test_una_sola_decision_pendiente_por_item(index, sample_file, tmp_path):
    """La bandeja debe mostrar cada archivo UNA vez con sus alternativas, no
    dos veces con destinos que se contradicen."""
    item_id = index.upsert_item(sample_file)
    a = index.add_folder(tmp_path / "a", "a")
    index.record_decision(item_id, [{"folder_id": a, "score": 0.9}], decided_by="semantic")

    with pytest.raises(sqlite3.IntegrityError):
        index.record_decision(item_id, [{"folder_id": a, "score": 0.4}], decided_by="semantic")


def test_un_patron_un_destino(index, tmp_path):
    """Dos reglas con el mismo patron y prioridad serian ambiguas: ganaria la
    que saliera primero, que es azar."""
    a = index.add_folder(tmp_path / "documentos", "documentos")
    b = index.add_folder(tmp_path / "facturas", "facturas")
    index.conn.execute(
        "INSERT INTO rules (match_type, pattern, folder_id) VALUES ('extension', '.pdf', ?)", (a,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        index.conn.execute(
            "INSERT INTO rules (match_type, pattern, folder_id) VALUES ('extension', '.pdf', ?)",
            (b,),
        )


def test_las_excepciones_no_cuentan_como_desacuerdo(index, sample_file, tmp_path):
    """Mover una factura a /impuestos porque toca la declaracion no significa
    que /facturas estuviera mal."""
    a = index.add_folder(tmp_path / "facturas", "facturas")
    b = index.add_folder(tmp_path / "impuestos", "impuestos")

    for nombre, verdict, excepcion in (
        ("uno.pdf", "accepted", False),
        ("dos.pdf", "corrected", False),
        ("tres.pdf", "corrected", True),  # excepcion: no es un fallo nuestro
    ):
        f = tmp_path / nombre
        f.write_bytes(nombre.encode())
        item_id = index.upsert_item(f)
        d = index.record_decision(item_id, [{"folder_id": a, "score": 0.8}],
                                  decided_by="semantic")
        index.settle_decision(d, verdict, b, is_exception=excepcion)

    r = index.agreement_rate()
    assert r["decididas"] == 2          # la excepcion queda fuera
    assert r["aceptadas"] == 1
    assert r["tasa_acuerdo"] == 0.5
