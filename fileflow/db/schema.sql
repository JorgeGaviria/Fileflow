-- Fileflow - esquema del indice
-- Ver docs/referencia/esquema-de-datos.md para el razonamiento de cada tabla.
--
-- Criterio de nombres: se evitan abreviaturas y jerga. Toda fecha termina en
-- _at. Las fechas que vienen del sistema de archivos llevan prefijo fs_ para
-- distinguirlas de las que genera Fileflow.
--
-- REGLA DEL SISTEMA: Fileflow no hace DELETE de sus propias entidades. Los
-- items, las carpetas y los directorios vigilados se marcan con status, nunca
-- se borran. Tres motivos:
--
--   1. El aprendizaje es la parte cara. Quitar /documentos una semana no debe
--      costar su centroide, sus ejemplares y su historial de decisiones:
--      volver a anadirla los recupera intactos (ON CONFLICT ... SET status).
--   2. SQLite reutiliza los ids de las filas borradas. Con borrado duro, la
--      siguiente carpeta creada puede heredar los vectores de una anterior
--      sin que nada falle -- clasificaria con el perfil equivocado en
--      silencio.
--   3. Coherencia: si Fileflow no borra archivos del usuario, tampoco deberia
--      borrar lo que aprendio de ellos.
--
-- Las clausulas ON DELETE se conservan como red de seguridad, aunque no
-- deberian dispararse nunca.
--
-- Consulta siempre las vistas active_* en vez de las tablas, salvo que
-- quieras el historico a proposito.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Metadatos del propio indice.
--
-- Guarda datos sobre la BASE, no sobre los archivos: con que version de
-- esquema y con que modelo se construyo. Va en una tabla y no en un fichero de
-- configuracion para que el indice sea autodescriptivo: abrirlo debe bastar
-- para saber que es.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Directorios vigilados: de donde salen los archivos nuevos.
--
-- subdir_policy sustituye a un booleano 'recursive', que se quedaba corto.
-- Que hacer con las carpetas que aparecen dentro:
--
--   unit     cada subcarpeta es UNA cosa, se clasifica y se mueve entera
--            (por defecto: extraer un zip no debe desperdigar su contenido)
--   ignore   no se mira dentro en absoluto
--   descend  cada archivo de dentro se clasifica por separado
--
-- El defecto es 'unit' porque 'descend' es destructivo con carpetas que el
-- usuario mantiene juntas a proposito.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watched_dirs (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    subdir_policy TEXT    NOT NULL DEFAULT 'unit'
                  CHECK (subdir_policy IN ('unit', 'ignore', 'descend')),
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disabled', 'deleted')),
    added_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE VIEW IF NOT EXISTS active_watched_dirs AS
    SELECT * FROM watched_dirs WHERE status = 'active';

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
--   Al marcar una madre como 'deleted', sus hijas se promocionan a raiz
--   (parent_id = NULL): quitar /imagenes de la configuracion no invalida
--   /imagenes/gatos como destino. OJO: como no hay DELETE, el ON DELETE SET
--   NULL no se dispara -- esa promocion la hace el codigo explicitamente.
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
    organize_by      TEXT    NOT NULL DEFAULT 'none'
                     CHECK (organize_by IN ('none', 'month', 'type', 'content')),
    is_trash         INTEGER NOT NULL DEFAULT 0,
    auto_move        INTEGER NOT NULL DEFAULT 0,
    auto_move_since  TEXT,
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'disabled', 'deleted')),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE VIEW IF NOT EXISTS active_folders AS
    SELECT * FROM folders WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

-- ---------------------------------------------------------------------------
-- Cosas clasificables. Se llama 'items' y no 'files' porque una carpeta
-- tratada como unidad es, para Fileflow, la misma clase de cosa que un
-- archivo: tiene ruta, tamano, fecha, vector y destino, y se mueve y se
-- deshace igual. Por eso comparten tabla en vez de tener pipelines paralelos.
--
-- 'path' es la ruta ACTUAL; el historico de donde estuvo esta en journal.
--
-- status: seis estados. Un estado existe solo si es PERSISTENTE y le importa
-- al usuario; lo transitorio y lo deducible no son estados.
--
--   pending    detectado, sin resolver
--   proposed   hay una decision esperando confirmacion
--   moved      colocado en su carpeta destino
--   ignored    excluido por el usuario o por regla
--   missing    ya no esta en disco (borrado o movido por fuera)
--   error      fallo al procesar, ver last_error. Sin este estado se
--              reintentaria en bucle un archivo que siempre falla.
--
-- Se descartaron dos: 'unstable' era transitorio (vive en el watcher mientras
-- el archivo se escribe, nunca llega a la base) y 'analyzed' era deducible
-- (o tiene fila en embeddings o no la tiene).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,

    item_type       TEXT    NOT NULL DEFAULT 'file'
                    CHECK (item_type IN ('file', 'dir')),
    child_count     INTEGER,                -- solo 'dir': cuantos archivos contiene

    path            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    extension       TEXT    NOT NULL DEFAULT '',
    size_bytes      INTEGER NOT NULL DEFAULT 0,

    -- Fechas del SISTEMA DE ARCHIVOS. Se guardan las dos porque significan
    -- cosas distintas y organize_by='month' tiene que elegir: una foto de 2019
    -- descargada hoy tiene fs_created_at de hoy y fs_modified_at de 2019 (si
    -- se preservo). Para fotos la fecha real esta en los metadatos EXIF; v2.
    fs_created_at   REAL,
    fs_modified_at  REAL    NOT NULL DEFAULT 0,

    -- Para 'file': blake2b parcial (cabecera + cola + tamano).
    -- Para 'dir' : hash del listado recursivo. Hace falta porque la fecha de
    -- una carpeta no cambia si se modifica un archivo dos niveles mas abajo.
    content_hash    TEXT,

    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'proposed', 'moved',
                                      'ignored', 'missing', 'error')),
    folder_id       INTEGER REFERENCES folders(id) ON DELETE SET NULL,

    -- Fechas de FILEFLOW: lo que hicimos nosotros.
    first_seen_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT    NOT NULL DEFAULT (datetime('now')),

    -- organized_at duplica informacion que ya esta en journal. Se acepta la
    -- copia porque deducirla exigiria buscar la entrada mas reciente del
    -- journal por cada fila de un listado de miles. REGLA: journal es la
    -- verdad; organized_at es una copia por comodidad. Si discrepan, manda
    -- journal.
    organized_at    TEXT,

    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_folder ON items(folder_id);
CREATE INDEX IF NOT EXISTS idx_items_hash   ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_type   ON items(item_type);

-- ---------------------------------------------------------------------------
-- Embeddings de los items.
--
-- model_id y dimensions van SIEMPRE con el vector: vectores de modelos
-- distintos no son comparables, y toda consulta filtra por model_id.
--
-- vector_space separa el espacio de texto del de imagen. Se llamaba 'kind',
-- pero ese nombre significaba tres cosas distintas en tres tablas.
--
-- extractor identifica CON QUE RECETA se obtuvo el contenido antes de
-- vectorizarlo ('pdf-text-v1', 'docx-v1', 'image-clip-v1'...). Un vector no
-- solo caduca si cambia el modelo: si manana el extractor de PDF aprende a
-- hacer OCR, todos los vectores de PDF escaneados pasan a ser basura -- se
-- generaron a partir de un texto vacio -- aunque el modelo sea el mismo.
-- Va como columna normal y no en la clave: de una receta vieja no queremos
-- conservar nada, queremos detectarla y regenerar.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    vector_space TEXT    NOT NULL,          -- 'text' | 'image'
    model_id     TEXT    NOT NULL,
    extractor    TEXT    NOT NULL DEFAULT '',
    dimensions   INTEGER NOT NULL,
    vector       BLOB    NOT NULL,          -- float32 little-endian, norma 1
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, vector_space, model_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_id, vector_space);

-- ---------------------------------------------------------------------------
-- Vectores de carpeta: las dos senales del scoring.
--
--   signal='description'  el embedding de lo que escribio el usuario
--   signal='centroid'     la media de lo que la carpeta ya contiene
--
-- sample_count solo aplica al centroide y alimenta el peso adaptativo
-- beta = n / (n + k) de la formula de scoring.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS folder_vectors (
    folder_id    INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    signal       TEXT    NOT NULL CHECK (signal IN ('description', 'centroid')),
    vector_space TEXT    NOT NULL,
    model_id     TEXT    NOT NULL,
    dimensions   INTEGER NOT NULL,
    vector       BLOB    NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (folder_id, signal, vector_space, model_id)
);

-- ---------------------------------------------------------------------------
-- Ejemplares: correcciones explicitas del usuario. La senal mas valiosa,
-- porque es supervision humana directa.
--
-- Van aparte y no diluidos en el centroide a proposito: el centroide es una
-- media (una correccion se pierde entre cientos de archivos), mientras que los
-- ejemplares se puntuan por MAXIMA similitud. Una correccion tiene que poder
-- cambiar el resultado ella sola.
-- ---------------------------------------------------------------------------
-- polarity guarda las DOS mitades de cada correccion. Si Fileflow propuso
-- /contratos y el usuario lo movio a /facturas, aprendemos que si va en
-- /facturas (positivo) y que no iba en /contratos (negativo).
--
--   Los positivos y los negativos NO valen lo mismo. Un positivo dice "esto se
--   parece a aquello" y generaliza bien. Un negativo puede ser una excepcion
--   disfrazada de regla: quiza esa factura fue a /impuestos solo porque el
--   usuario estaba haciendo la declaracion, y penalizar /facturas por eso
--   estropearia las siguientes.
--
--   Por eso en v1 los negativos se GUARDAN pero NO entran en el scoring. Se
--   usan para calibrar el umbral de la carpeta: si /contratos acumula muchas
--   propuestas rechazadas, lo que hay que subir es su umbral, no penalizar
--   archivo por archivo. Ver docs/diseno/motor-de-decision.md
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exemplars (
    id           INTEGER PRIMARY KEY,
    folder_id    INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    polarity     TEXT    NOT NULL DEFAULT 'positive'
                 CHECK (polarity IN ('positive', 'negative')),
    vector_space TEXT    NOT NULL,
    model_id     TEXT    NOT NULL,
    dimensions   INTEGER NOT NULL,
    vector       BLOB    NOT NULL,
    source_path  TEXT,                      -- de que item salio, para trazabilidad
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exemplars_folder
    ON exemplars(folder_id, model_id, vector_space);

-- Higiene, no correccion: como los ejemplares se puntuan por MAXIMA similitud,
-- tener el mismo vector cinco veces da el mismo maximo que tenerlo una. Solo
-- gasta espacio y tiempo. Importaria si algun dia se promediaran.
CREATE UNIQUE INDEX IF NOT EXISTS idx_exemplars_unicos
    ON exemplars(folder_id, source_path, vector_space, model_id, polarity);

-- ---------------------------------------------------------------------------
-- Reglas rapidas (etapa 1 del pipeline). priority mayor gana; las que crea el
-- usuario desde la bandeja entran con prioridad alta.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY,
    match_type  TEXT    NOT NULL CHECK (match_type IN ('extension', 'glob', 'regex')),
    pattern     TEXT    NOT NULL,
    folder_id   INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT    NOT NULL DEFAULT 'user' CHECK (created_by IN ('user', 'builtin')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Un patron, un destino. Sin esto se pueden crear dos reglas con el mismo
-- patron y la misma prioridad apuntando a carpetas distintas, y entonces gana
-- la que salga primero, que es azar. Para cambiar el destino se edita la
-- regla, no se crea una segunda.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_patron ON rules(match_type, pattern);

-- ---------------------------------------------------------------------------
-- Decisiones: que se propuso y que paso.
--
-- candidates_json guarda el TOP-5 COMPLETO con sus scores, no solo la carpeta
-- elegida. Es lo que permite distinguir despues un fallo de calibracion (la
-- correcta estaba en el puesto 2) de uno de representacion (no estaba en la
-- lista). Sin eso solo sabes que fallaste, no por que.
--
-- decided_by dice que etapa del pipeline resolvio: regla rapida, similitud
-- semantica, LLM o descarte por defecto.
--
-- is_exception distingue "te equivocaste" de "esta vez quiero otra cosa". Al
-- corregir, el usuario elige entre 'solo este archivo' y 'siempre que se
-- parezca'. Una excepcion no genera ejemplar y no cuenta como desacuerdo en
-- las metricas: mover una factura a /impuestos porque toca la declaracion no
-- significa que /facturas estuviera mal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id                 INTEGER PRIMARY KEY,
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    candidates_json    TEXT    NOT NULL,    -- [{folder_id, score, alpha, beta}]
    proposed_folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    confidence         REAL    NOT NULL DEFAULT 0,
    margin             REAL    NOT NULL DEFAULT 0,  -- score#1 - score#2
    decided_by         TEXT    NOT NULL
                       CHECK (decided_by IN ('rule', 'semantic', 'llm', 'fallback')),
    model_id           TEXT,
    profile            TEXT,
    verdict            TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (verdict IN ('pending', 'accepted', 'corrected', 'rejected')),
    is_exception       INTEGER NOT NULL DEFAULT 0,
    final_folder_id    INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_verdict ON decisions(verdict);
CREATE INDEX IF NOT EXISTS idx_decisions_item    ON decisions(item_id);

-- Una sola decision pendiente por item. Las "varias sugerencias" son el top-5
-- dentro de candidates_json, no varias filas: la bandeja debe mostrar cada
-- archivo UNA vez con sus alternativas, no dos veces con destinos que se
-- contradicen.
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_una_pendiente
    ON decisions(item_id) WHERE verdict = 'pending';

-- ---------------------------------------------------------------------------
-- Journal: toda operacion sobre el sistema de archivos pasa por aqui, ANTES
-- de ejecutarse. Es lo que hace que cualquier movimiento sea reversible.
--
-- Escribir la intencion antes y no despues es lo que lo hace util: si el
-- proceso muere a mitad, quedan entradas en 'planned' y se sabe exactamente
-- que quedo a medias y donde mirar.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal (
    id           INTEGER PRIMARY KEY,
    operation    TEXT    NOT NULL
                 CHECK (operation IN ('move', 'rename', 'mkdir', 'trash')),
    source_path  TEXT,
    dest_path    TEXT,
    item_id      INTEGER REFERENCES items(id) ON DELETE SET NULL,
    decision_id  INTEGER REFERENCES decisions(id) ON DELETE SET NULL,
    state        TEXT    NOT NULL DEFAULT 'planned'
                 CHECK (state IN ('planned', 'done', 'failed', 'undone')),
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    done_at      TEXT,
    undone_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_journal_state ON journal(state);
CREATE INDEX IF NOT EXISTS idx_journal_time  ON journal(created_at);
