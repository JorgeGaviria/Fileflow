-- Fileflow - esquema del indice
-- Ver docs/esquema-de-datos.md para el razonamiento detras de cada tabla.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Metadatos del propio indice (version de esquema, perfil activo, etc.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Directorios vigilados: de donde salen los archivos nuevos.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watched_dirs (
    id         INTEGER PRIMARY KEY,
    path       TEXT    NOT NULL UNIQUE,
    recursive  INTEGER NOT NULL DEFAULT 1,
    enabled    INTEGER NOT NULL DEFAULT 1,
    added_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Carpetas destino: a donde pueden ir los archivos. La descripcion en lenguaje
-- natural es la senal principal mientras la carpeta esta vacia.
--
-- parent_id hace la tabla jerarquica: /imagenes/gatos apunta a /imagenes. Se
-- guarda explicito en vez de deducirlo comparando rutas como texto, que es
-- fragil con mayusculas, separadores y renombrados. NULL = carpeta raiz.
--
--   Regla de precedencia: ante un archivo que encaja en madre e hija, GANA LA
--   HIJA. Si el usuario creo /imagenes/gatos es porque quiere las fotos de
--   gatos ahi, no repartidas por la estrategia de /imagenes.
--
--   ON DELETE SET NULL, no CASCADE: quitar /imagenes de la configuracion no
--   significa que /imagenes/gatos deje de ser un destino valido. La hija se
--   promociona a raiz.
--
-- organize_by dice como subdividir en subcarpetas lo que caiga aqui y no
-- encaje en ninguna hija. Aplica a CUALQUIER carpeta, no solo a la papelera:
-- /imagenes puede querer agrupar por mes igual que /Unsorted.
--
-- is_trash marca las carpetas que aceptan lo que nadie mas quiso. Puede haber
-- varias: si ninguna carpeta normal supera el umbral, se puntua SOLO entre las
-- papeleras y gana la mejor SIN umbral -- alguien tiene que quedarse el
-- archivo. Su description sirve para repartir entre ellas.
--
-- auto_move + auto_move_since implementan la confianza graduada: el modo
-- automatico se activa por carpeta, cuando se ha ganado con aciertos.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS folders (
    id               INTEGER PRIMARY KEY,
    parent_id        INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    path             TEXT    NOT NULL UNIQUE,
    description      TEXT    NOT NULL DEFAULT '',
    organize_by      TEXT    NOT NULL DEFAULT 'none',  -- none|month|type|content
    enabled          INTEGER NOT NULL DEFAULT 1,
    auto_move        INTEGER NOT NULL DEFAULT 0,
    auto_move_since  TEXT,
    is_trash         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

-- ---------------------------------------------------------------------------
-- Archivos conocidos. 'path' es la ruta ACTUAL; el historico esta en journal.
--
-- status:
--   pending    detectado, aun no analizado
--   unstable   sigue creciendo (descarga en curso), no tocar
--   analyzed   ya tiene embedding
--   proposed   hay una decision esperando confirmacion
--   moved      colocado en su carpeta destino
--   ignored    excluido por el usuario o por regla
--   missing    ya no esta en disco (borrado o movido por fuera)
--   error      fallo al procesar, ver last_error
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    ext           TEXT    NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    mtime         REAL    NOT NULL DEFAULT 0,
    content_hash  TEXT,                    -- blake2b parcial, para detectar duplicados
    status        TEXT    NOT NULL DEFAULT 'pending',
    folder_id     INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    first_seen    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT    NOT NULL DEFAULT (datetime('now')),
    last_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_hash   ON files(content_hash);

-- ---------------------------------------------------------------------------
-- Embeddings. model_id y dim van SIEMPRE con el vector: vectores de modelos
-- distintos no son comparables, y toda consulta filtra por model_id.
-- kind distingue el espacio vectorial (texto vs imagen), que tampoco se mezclan.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,           -- 'text' | 'image'
    model_id   TEXT    NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB    NOT NULL,           -- float32 little-endian, norma 1
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (file_id, kind, model_id)
);

CREATE INDEX IF NOT EXISTS idx_emb_model ON embeddings(model_id, kind);

-- ---------------------------------------------------------------------------
-- Vectores de carpeta: descripcion y centroide, las dos senales del scoring.
-- source: 'description' | 'centroid'
-- n_samples solo aplica al centroide y alimenta el peso adaptativo beta.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS folder_vectors (
    folder_id   INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    model_id    TEXT    NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB    NOT NULL,
    n_samples   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (folder_id, source, kind, model_id)
);

-- ---------------------------------------------------------------------------
-- Ejemplares: correcciones explicitas del usuario. La senal mas valiosa,
-- porque es supervision humana directa.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exemplars (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    model_id    TEXT    NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB    NOT NULL,
    source_file TEXT,                      -- de que archivo salio, para trazabilidad
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exemplars_folder ON exemplars(folder_id, model_id, kind);

-- ---------------------------------------------------------------------------
-- Reglas rapidas (etapa 1). priority mayor gana; las creadas por el usuario
-- desde la bandeja entran con prioridad alta.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,           -- 'ext' | 'glob' | 'regex'
    pattern    TEXT    NOT NULL,
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    priority   INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_by TEXT    NOT NULL DEFAULT 'user',  -- 'user' | 'builtin'
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Decisiones. Se guarda el TOP-5 COMPLETO en 'candidates' (JSON), no solo la
-- elegida: es lo que permite distinguir un fallo de calibracion (la correcta
-- era la #2) de uno de representacion (no estaba en la lista).
--
-- verdict: pending | accepted | corrected | rejected
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id                 INTEGER PRIMARY KEY,
    file_id            INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    candidates         TEXT    NOT NULL,   -- JSON: [{folder_id, score, alpha, beta}]
    proposed_folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    confidence         REAL    NOT NULL DEFAULT 0,
    margin             REAL    NOT NULL DEFAULT 0,  -- score#1 - score#2
    stage              TEXT    NOT NULL,   -- 'rule' | 'semantic' | 'llm' | 'fallback'
    model_id           TEXT,
    profile            TEXT,
    verdict            TEXT    NOT NULL DEFAULT 'pending',
    final_folder_id    INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_verdict ON decisions(verdict);
CREATE INDEX IF NOT EXISTS idx_decisions_file    ON decisions(file_id);

-- ---------------------------------------------------------------------------
-- Journal: toda operacion sobre el sistema de archivos pasa por aqui, ANTES
-- de ejecutarse. Es lo que hace que cualquier movimiento sea reversible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY,
    op          TEXT    NOT NULL,          -- 'move' | 'rename' | 'mkdir' | 'trash'
    src         TEXT,
    dst         TEXT,
    file_id     INTEGER REFERENCES files(id) ON DELETE SET NULL,
    decision_id INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
    state       TEXT    NOT NULL DEFAULT 'planned',  -- planned|done|failed|undone
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    done_at     TEXT,
    undone_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_journal_state ON journal(state);
CREATE INDEX IF NOT EXISTS idx_journal_time  ON journal(created_at);
