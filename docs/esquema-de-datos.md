# Esquema de datos

Todo el estado vive en **un único archivo SQLite** (`.fileflow/index.db`). Sin servidor,
sin proceso aparte, sin dependencias externas. Coherente con el objetivo de que la
aplicación sea local y ligera.

El DDL está en [`fileflow/db/schema.sql`](../fileflow/db/schema.sql). Este documento
explica el *porqué* de cada pieza.

## Por qué SQLite y no una base vectorial

Conviene separar dos cosas que se confunden a menudo:

- **Producir** el embedding es trabajo del modelo (e5, CLIP). Una base vectorial no genera
  vectores.
- **Guardar y buscar** los embeddings es lo único que decide esta elección.

La tentación es meter Chroma, Qdrant o similar. Medido, no compensa a esta escala.

### Medición

Cifras reales tomadas en **Maria** (i5-12450H, 8 GB, sin GPU) — la máquina más limitada de
las dos, es decir, el peor caso. Reproducible con
[`tests/bench_vectors.py`](../tests/bench_vectors.py):

```
100 000 embeddings de 384 dimensiones (perfil ligero)

Escritura en SQLite         2,4 - 3,1 s      (32 000 - 41 000 vectores/s)
Tamaño en disco             200 MB
Carga a memoria             1,3 - 1,8 s      147 MB de RAM

[A] Clasificar 1 archivo contra 50 carpetas      3 - 8 microsegundos
[B] Búsqueda semántica sobre los 100 000         4 - 6 ms
```

Son rangos de varias ejecuciones: la primera es más lenta por cachés en frío. Aun tomando
el peor valor de cada rango, la conclusión no cambia.

### Lo que dicen esos números

**El caso A es el que ejecuta Fileflow constantemente, y cuesta microsegundos.** Generar
el embedding de ese mismo archivo costará entre 50 y 200 ms. La búsqueda es unas **10 000
veces más barata que producir el vector**.

Añadir una base vectorial optimizaría la parte que no cuesta nada, dejando intacto el
cuello de botella real, a cambio de una dependencia más, un servicio que gestionar y
complejidad al empaquetar. Un índice ANN (HNSW) solo empieza a ganar por encima del millón
de vectores, y aun entonces a costa de dar resultados aproximados.

Además, clasificar **no compara contra todos los archivos**: compara contra las carpetas,
que son decenas. Solo la búsqueda semántica de v2 necesita recorrerlo todo, y ahí la fuerza
bruta responde en unos 5 ms, que para una persona es instantáneo.

### Dónde está el límite

El límite no es el tiempo, es la **RAM**, y solo afecta a la búsqueda semántica (el único
caso que carga todos los vectores a la vez):

| Archivos | Disco | RAM al cargar todo | Búsqueda |
|---|---|---|---|
| 20 000 | 29 MB | 29 MB | ~1 ms |
| 100 000 | 147 MB | 147 MB | ~5 ms |
| 500 000 | 732 MB | 732 MB | ~25 ms |

Con 100 000 archivos son 147 MB, en torno al 10 % del presupuesto de Maria: asumible. Con
500 000 ya molesta.

Si se llegara ahí, la respuesta **no** es una base vectorial: es guardar en `float16` en
lugar de `float32`. La mitad de RAM, y la pérdida de precisión en similitud coseno es
despreciable. Es un cambio de una línea en `pack_vector`.

### Formato de almacenamiento

Los vectores se guardan como `BLOB`: float32 little-endian, **ya normalizados a norma 1**.
Se normaliza al guardar para que la similitud coseno sea después un simple producto
escalar, sin divisiones — es la operación más repetida de todo el sistema.

Si algún día hiciera falta, `sqlite-vec` se puede añadir sin cambiar el esquema: es una
extensión sobre el mismo motor, no otra base de datos.

## Las tablas

### `files` — qué existe y en qué estado está

Una fila por archivo conocido. `path` es la ruta **actual**; el histórico de dónde estuvo
está en `journal`.

El campo `status` es la máquina de estados del pipeline:

```
pending ──> unstable ──> analyzed ──> proposed ──> moved
                             │                       │
                             └──> ignored            └──> missing
```

`content_hash` es un blake2b **parcial** (cabecera + cola + tamaño, no el archivo entero):
suficiente para detectar duplicados sin leer gigabytes. Detectar que un archivo ya está
indexado en otra ruta evita reprocesarlo y permite avisar de descargas repetidas.

### `folders` — a dónde puede ir

`description` es la señal en lenguaje natural, y es lo único que existe cuando la carpeta
está vacía. Es la pieza que resuelve el arranque en frío
(ver [motor de decisión](motor-de-decision.md)).

`auto_move` y `auto_move_since` implementan la **confianza graduada**: el modo automático
no es un ajuste global, se activa carpeta por carpeta cuando esa carpeta ha demostrado un
histórico de aciertos. `is_trash` marca la carpeta de descarte (`/Unsorted`).

### `embeddings` — los vectores de los archivos

La clave primaria es `(file_id, kind, model_id)`, y eso es deliberado:

- **`model_id` en la clave** permite convivencia. Un archivo puede tener su vector de
  `e5-small` y el de `bge-m3` a la vez, lo que hace que cambiar de perfil sea incremental
  en lugar de un borrado total.
- **`kind`** separa el espacio de texto del de imagen. Son espacios vectoriales distintos
  y comparar entre ellos no tiene sentido.
- **`dim` guardado explícitamente** permite validar al leer. Si la dimensión no coincide
  con la esperada, hay un bug o un índice corrupto, y es mejor fallar ruidosamente que
  comparar basura en silencio.

> **Regla invariante: toda consulta de similitud filtra por `model_id` y por `kind`.**
> Vectores de modelos distintos no producen un resultado malo, producen un resultado sin
> sentido. Ver [modelos y perfiles](modelos-y-perfiles.md).

### `folder_vectors` — las dos señales del scoring

Misma estructura, pero por carpeta, con `source` distinguiendo `description` de `centroid`.

`n_samples` cuenta cuántos archivos componen el centroide, y **no es solo informativo**:
alimenta directamente el peso adaptativo `β = n / (n + k)` de la fórmula de scoring.

El centroide se actualiza de forma incremental cuando se confirma un archivo, sin
recalcular la carpeta entera:

```
c_nuevo = normalizar( (c_viejo · n + v_archivo) / (n + 1) )
```

### `exemplars` — las correcciones del usuario

Cada vez que el usuario corrige una propuesta, el vector de ese archivo se guarda apuntando
a la carpeta correcta.

Están en una tabla aparte y no diluidos dentro del centroide **a propósito**: el centroide
es una media (una corrección se diluye entre cientos de archivos), mientras que los
ejemplares se puntúan por **máxima similitud**. Una corrección tiene que poder cambiar el
resultado ella sola, porque es supervisión humana directa.

### `rules` — la etapa rápida

Reglas deterministas por extensión, glob o regex. Las que crea el usuario desde la bandeja
("todos los `.torrent` a esta carpeta, siempre") entran con prioridad alta y ganan a las
predefinidas.

### `decisions` — el registro de lo que se propuso y qué pasó

La tabla más importante para mejorar el sistema, y la razón es el campo `candidates`, que
guarda el **top-5 completo con sus scores** en JSON, no solo la carpeta elegida.

Sin eso solo sabes que fallaste. Con eso sabes por qué:

| Dónde estaba la correcta | Diagnóstico | Acción |
|---|---|---|
| Puesto #2 o #3 | Calibración | Ajustar umbrales y pesos |
| Fuera del top-5 | Representación | Mejor modelo, extracción o descripción |

`margin` (la diferencia entre el primero y el segundo) se guarda ya calculado porque es el
criterio de ambigüedad: dos candidatos casi empatados son una señal de duda aunque el score
absoluto sea alto.

`model_id` y `profile` quedan registrados en cada decisión para poder comparar
honestamente el rendimiento entre perfiles y entre las dos máquinas de desarrollo.

### `journal` — la red de seguridad

**Toda** operación sobre el sistema de archivos se escribe aquí *antes* de ejecutarse, con
`state='planned'`, y se marca `done` después. Ver
[watcher y seguridad](watcher-y-seguridad.md) para el protocolo completo y el undo.

## Migraciones

`meta.schema_version` guarda la versión. Al abrir el índice se aplican en orden las
migraciones pendientes desde `fileflow/db/migrations/`.

Mientras el proyecto esté en desarrollo temprano, la ruta soportada para un cambio de
esquema es **borrar `.fileflow/` y reindexar**. Es aceptable porque el índice es una caché
reconstruible: la información que importa está en los archivos del usuario, no en la base
de datos.

## Qué NO se versiona ni se sincroniza

`.fileflow/` está en `.gitignore` completo. El índice es **local a cada máquina**:

- Depende del perfil de hardware, y las dos máquinas de desarrollo usan perfiles distintos,
  con modelos y dimensiones distintas.
- Contiene rutas absolutas de esa máquina.
- Contiene el contenido personal del usuario, que jamás debe salir en un commit.
