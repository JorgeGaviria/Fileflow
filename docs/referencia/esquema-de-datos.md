# Esquema de datos

Todo el estado vive en **un único archivo SQLite** (`.fileflow/index.db`). Sin servidor,
sin proceso aparte, sin dependencias externas. Coherente con el objetivo de que la
aplicación sea local y ligera.

El DDL está en [`fileflow/db/schema.sql`](../../fileflow/db/schema.sql) y el diagrama
entidad-relación en [diagramas/esquema-relacional.md](../diagramas/esquema-relacional.md).
Este documento explica el *porqué* de cada pieza.

## Dos reglas que atraviesan todo el esquema

**1. Fileflow no hace `DELETE` de sus propias entidades.** `items`, `folders` y
`watched_dirs` se dan de baja con `status`, nunca se borran. Tres motivos, por orden de
importancia:

- **El aprendizaje es la parte cara.** Quitar `/documentos` de la configuración una semana
  no debe costar su centroide, sus ejemplares y su historial. Volver a añadirla reactiva
  la misma fila y lo recupera intacto.
- **SQLite reutiliza los identificadores de las filas borradas.** Con borrado duro, la
  siguiente carpeta creada puede heredar los vectores de una anterior, y clasificaría con
  el perfil equivocado sin que nada fallara. Está verificado, no es teórico.
- **Coherencia.** Si Fileflow no borra archivos del usuario, tampoco debería borrar lo que
  aprendió de ellos.

Las vistas `active_folders` y `active_watched_dirs` existen para que el filtro no dependa
de acordarse en cada consulta.

**2. Los vectores nunca se mezclan.** Un vector se guarda siempre con su `model_id`, su
`vector_space` y su `extractor`, y toda consulta de similitud filtra por ellos. Vectores de
modelos distintos no dan un resultado malo: dan un resultado **sin sentido**.

## Nombres

Sin abreviaturas ni jerga. Toda fecha termina en `_at`, y las que vienen del sistema de
archivos llevan el prefijo `fs_` para no confundirlas con las que genera Fileflow.

El criterio nació de un problema real: `kind` significaba tres cosas distintas en tres
tablas (archivo/carpeta, texto/imagen, extensión/glob/regex), lo que hacía imposible leer
una consulta y saber de qué se hablaba.

## Por qué SQLite y no una base vectorial

Conviene separar dos cosas que se confunden a menudo:

- **Producir** el embedding es trabajo del modelo (e5, CLIP). Una base vectorial no genera
  vectores.
- **Guardar y buscar** los embeddings es lo único que decide esta elección.

### Medición

Cifras reales tomadas en **Maria** (i5-12450H, 8 GB, sin GPU) — la máquina más limitada de
las dos, es decir, el peor caso. Reproducible con
[`tests/bench_vectors.py`](../../tests/bench_vectors.py):

```
100 000 embeddings de 384 dimensiones (perfil ligero)

Escritura en SQLite         2,4 - 3,1 s      (32 000 - 41 000 vectores/s)
Tamaño en disco             200 MB
Carga a memoria             1,3 - 1,8 s      147 MB de RAM

[A] Clasificar 1 archivo contra 50 carpetas      3 - 8 microsegundos
[B] Búsqueda semántica sobre los 100 000         4 - 6 ms
```

**El caso A es el que ejecuta Fileflow constantemente, y cuesta microsegundos.** Generar el
embedding de ese mismo archivo costará entre 50 y 200 ms: la búsqueda es unas **10 000
veces más barata que producir el vector**. Añadir una base vectorial optimizaría la parte
que no cuesta nada.

Además, clasificar **no compara contra todos los archivos**: compara contra las carpetas,
que son decenas.

### Dónde está el límite

El límite no es el tiempo, es la **RAM**, y solo afecta a la búsqueda semántica de v2, que
es el único caso que carga todos los vectores a la vez:

| Archivos | Disco | RAM al cargar todo | Búsqueda |
|---|---|---|---|
| 20 000 | 29 MB | 29 MB | ~1 ms |
| 100 000 | 147 MB | 147 MB | ~5 ms |
| 500 000 | 732 MB | 732 MB | ~25 ms |

Con 500 000 archivos ya molesta. Si se llegara ahí, la respuesta **no** es una base
vectorial: es guardar en `float16`, que es la mitad de RAM y una pérdida de precisión
despreciable en similitud coseno.

Los vectores se guardan como `BLOB`: float32 little-endian, **ya normalizados a norma 1**,
para que la similitud coseno sea un simple producto escalar sin divisiones.

---

## Las tablas

### `meta` — la etiqueta del índice

Tabla clave-valor con información sobre **la base misma**, no sobre los archivos: con qué
versión de esquema y con qué modelo se construyó.

Va en una tabla y no en un fichero de configuración para que el índice sea
**autodescriptivo**: abrirlo debe bastar para saber qué es. Si `schema_version` viviera
fuera, bastaría con borrar ese fichero para perder la relación entre la base y el código
que la entiende.

El orden importa: la versión se comprueba **antes** de aplicar el esquema. Con
`CREATE TABLE IF NOT EXISTS`, una base antigua conserva sus tablas viejas y el fallo
aparecería mucho después como un críptico `no such column`.

### `watched_dirs` — de dónde vienen los archivos

`subdir_policy` sustituye a un booleano `recursive` que se quedaba corto:

| Valor | Qué hace |
|---|---|
| `unit` | Cada subcarpeta es **una** cosa, se clasifica y se mueve entera ← **por defecto** |
| `ignore` | No se mira dentro en absoluto |
| `descend` | Cada archivo de dentro se clasifica por separado |

El defecto es `unit` porque `descend` es destructivo: extraer un `.zip` en Descargas
repartiría `index.html`, `logo.png` y `manual.pdf` por tres carpetas distintas, deshaciendo
una carpeta que el usuario mantiene junta a propósito.

### `folders` — a dónde puede ir

`description` es la señal en lenguaje natural, y es lo único que existe cuando la carpeta
está vacía. Resuelve el arranque en frío (ver [motor de decisión](../diseno/motor-de-decision.md)).

**`parent_id` hace la tabla jerárquica.** `/imagenes/gatos` apunta a `/imagenes`. Se guarda
explícito en vez de deducirlo comparando rutas como texto, que es frágil con mayúsculas,
separadores y renombrados. La tabla se referencia a sí misma.

- **Precedencia:** gana el candidato **más profundo de entre los que superan el umbral** —
  no el que más puntúe. Así una subcarpeta específica nunca pierde contra su madre, y la
  madre sigue recogiendo lo que a las hijas no les encaja.
- **Al dar de baja una madre, sus hijas se promocionan a raíz.** Quitar `/imagenes` de la
  configuración no invalida `/imagenes/gatos` como destino. Esa promoción la hace el código
  explícitamente: como no hay `DELETE`, el `ON DELETE SET NULL` nunca se dispara.

**`organize_by`** dice cómo subdividir en subcarpetas lo que caiga aquí y no encaje en
ninguna hija — organiza el **residuo**. Aplica a *cualquier* carpeta, no solo a la
papelera: `/imagenes` puede querer agrupar por mes igual que `/Unsorted`.

**`is_trash`** marca las carpetas que aceptan lo que nadie más quiso. **Puede haber
varias**: si ninguna carpeta normal supera el umbral, se puntúa solo entre las papeleras y
gana la mejor **sin umbral** — alguien tiene que quedarse el archivo. Su `description`
sirve para repartir entre ellas.

**`auto_move`** implementa la confianza graduada: el modo automático se activa carpeta por
carpeta, cuando esa carpeta ha demostrado un histórico de aciertos.

### `items` — las cosas clasificables

Se llama `items` y no `files` porque **una carpeta tratada como unidad es, para Fileflow,
la misma clase de cosa que un archivo**: tiene ruta, tamaño, fecha, vector y destino, y se
mueve y se deshace igual. Por eso comparten tabla en vez de tener pipelines paralelos, y
`embeddings`, `decisions` y `journal` sirven para ambos sin tocar nada.

Ese es el criterio de que el modelado es correcto: si hubiera hecho falta duplicar media
docena de tablas, estaríamos forzando el diseño.

`item_type` distingue `file` de `dir`. Para un `dir`, `content_hash` cambia de significado:
es el hash del **listado recursivo**, no del contenido. Hace falta porque la fecha de una
carpeta en Windows no cambia si se modifica un archivo dos niveles más abajo.

**Los seis estados.** Un estado existe solo si es **persistente** y le importa al usuario:

| | |
|---|---|
| `pending` | Detectado, sin resolver |
| `proposed` | Hay una decisión esperando confirmación |
| `moved` | Colocado en su carpeta destino |
| `ignored` | Excluido por el usuario o por regla |
| `missing` | Ya no está en disco |
| `error` | Falló al procesar. Sin este estado se reintentaría en bucle |

Se descartaron dos: `unstable` era **transitorio** (vive en el watcher mientras el archivo
se escribe, nunca llega a la base) y `analyzed` era **deducible** (o tiene fila en
`embeddings` o no la tiene).

**Las fechas son de dos naturalezas**, y por eso se distinguen con el prefijo:

| | Columna | Qué dice |
|---|---|---|
| Del sistema | `fs_created_at` | Cuándo se creó en el disco |
| | `fs_modified_at` | Cuándo se modificó su contenido |
| De Fileflow | `first_seen_at` | Cuándo lo vimos por primera vez |
| | `last_seen_at` | Última vez que confirmamos que sigue ahí |
| | `organized_at` | Cuándo lo colocamos nosotros |

Se guardan las dos del sistema porque significan cosas distintas y `organize_by='month'`
tiene que elegir: una foto de 2019 descargada hoy tiene `fs_created_at` de hoy y
`fs_modified_at` de 2019, si se preservó. Para fotos la fecha real está en los metadatos
EXIF, pero eso es v2.

> `organized_at` duplica información que ya está en `journal`. Se acepta la copia porque
> deducirla exigiría buscar la entrada más reciente del journal por cada fila de un listado
> de miles. **Regla: `journal` es la verdad, `organized_at` es una copia por comodidad.**

### `embeddings` — los vectores de los items

La clave primaria es `(item_id, vector_space, model_id)`, y eso es deliberado: **con
`model_id` en la clave, un item puede tener a la vez su vector de `e5-small` y el de
`bge-m3`**, lo que hace que cambiar de perfil sea incremental en lugar de un borrado total.

`extractor` identifica **con qué receta** se obtuvo el contenido antes de vectorizarlo
(`pdf-text-v1`, `image-clip-v1`...). Un vector caduca por dos motivos, no uno: si mañana el
extractor de PDF aprende a hacer OCR, **todos los vectores de PDF escaneados pasan a ser
basura** —se generaron a partir de un texto vacío— aunque el modelo sea el mismo. Va como
columna normal y no en la clave porque de una receta vieja no queremos conservar nada,
queremos detectarla y regenerar.

### `folder_vectors` — las dos señales del scoring

`signal` distingue `description` de `centroid`. `sample_count` cuenta cuántos items
componen el centroide y **no es informativo**: alimenta el peso adaptativo `β = n/(n+k)`.

El centroide se actualiza de forma incremental, sin recalcular la carpeta entera:

```
c_nuevo = normalizar( (c_viejo · n + v_item) / (n + 1) )
```

> **El centroide de una carpeta contiene solo lo que aterrizó ahí directamente**, no lo que
> hay en sus hijas. Propagar hacia arriba parecía buena idea y resulta que rompe el
> sistema: convierte a la madre en una copia de sus hijas y le quita la capacidad de
> recoger lo que no encaja en ninguna. El razonamiento completo está en
> [motor de decisión](../diseno/motor-de-decision.md).

### `exemplars` — las correcciones del usuario

Van aparte y no diluidos en el centroide **a propósito**: el centroide es una media, donde
una corrección se pierde entre cientos de archivos, mientras que los ejemplares se puntúan
por **máxima** similitud. Una corrección tiene que poder cambiar el resultado ella sola.

`polarity` guarda **las dos mitades** de cada corrección: si Fileflow propuso `/contratos`
y el usuario lo movió a `/facturas`, aprendemos que sí va en `/facturas` (positivo) y que
no iba en `/contratos` (negativo).

**Pero no valen lo mismo.** Un positivo dice "esto se parece a aquello" y generaliza bien.
Un negativo puede ser una excepción disfrazada de regla: quizá esa factura fue a
`/impuestos` solo porque el usuario estaba haciendo la declaración. Por eso en v1 los
negativos **se guardan pero no entran en el scoring**; se usan para calibrar el umbral de
la carpeta.

Hay un índice único sobre `(folder_id, source_path, vector_space, model_id, polarity)`,
pero es **higiene, no corrección**: como se puntúa por máximo, tener el mismo vector cinco
veces da el mismo resultado que tenerlo una. Solo gasta espacio. Importaría si algún día se
promediaran.

### `rules` — la etapa rápida

Reglas deterministas por extensión, glob o regex, sin leer el contenido.

**Un patrón, un destino.** Hay un índice único sobre `(match_type, pattern)` porque dos
reglas con el mismo patrón y la misma prioridad apuntando a carpetas distintas son
ambiguas: ganaría la que saliera primero, que es azar. Para cambiar el destino se edita la
regla, no se crea una segunda.

### `decisions` — qué se propuso y qué pasó

La tabla más importante para mejorar el sistema, y la razón es `candidates_json`, que
guarda el **top-5 completo con sus scores**, no solo la carpeta elegida:

| Dónde estaba la correcta | Diagnóstico | Acción |
|---|---|---|
| Puesto #2 o #3 | Calibración | Ajustar umbrales y pesos |
| Fuera del top-5 | Representación | Mejor modelo, extracción o descripción |

Sin eso solo sabes que fallaste, no por qué.

**Una sola decisión pendiente por item**, garantizada con un índice único parcial. Las
"varias sugerencias" son el top-5 *dentro* de una decisión: la bandeja debe mostrar cada
archivo una vez con sus alternativas, no dos veces con destinos que se contradicen.

`is_exception` distingue **"te equivocaste"** de **"esta vez quiero otra cosa"**. Al
corregir, el usuario elige entre *solo este archivo* y *siempre que se parezca*. Una
excepción no genera ejemplar y no cuenta como desacuerdo en las métricas.

`margin` (la diferencia entre el primero y el segundo) se guarda ya calculado porque es el
criterio de ambigüedad: dos candidatos casi empatados son señal de duda aunque el score
absoluto sea alto.

### `journal` — la red de seguridad

**Toda** operación sobre el sistema de archivos se escribe aquí *antes* de ejecutarse, con
`state='planned'`, y se marca `done` después. Escribir la intención antes y no después es
lo que lo hace útil: si el proceso muere a mitad, quedan entradas en `planned` y se sabe
exactamente qué quedó a medias.

Es un **registro append-only**: deshacer no modifica la entrada vieja, crea una nueva con
las rutas invertidas y `undoes_id` apuntando a la original. Por eso `undone` no es un
estado — `state` describe el ciclo de vida de *esa* operación, y que haya sido revertida se
deduce de que exista otra entrada que la revierta.

`batch_id` agrupa una misma confirmación, que es lo que permite deshacer un lote entero en
vez de operación por operación. Y `trash` queda fuera de las operaciones reversibles:
sacar algo de la papelera de Windows con código no es viable.

Ver [watcher y seguridad](watcher-y-seguridad.md) para el protocolo completo y el undo.

---

## Sobre unificar las tres tablas de vectores

`embeddings`, `folder_vectors` y `exemplars` se parecen mucho, y es razonable preguntarse
si deberían ser una sola tabla con un dueño polimórfico. Se consideró y se descartó.

**A favor de unificar:** reindexar al cambiar de perfil sería una consulta en vez de tres —
y olvidar una dejaría vectores del modelo antiguo mezclados, que es el fallo del que más
nos protegemos. Además, añadir un cuarto tipo de vector no costaría una tabla.

**A favor de separar:**

- **Claves foráneas de verdad.** Con un dueño polimórfico, un vector que apunta a una
  carpeta que nunca existió se acepta sin protestar. La baja lógica evita los huérfanos por
  borrado, pero no este caso.
- **Cada regla de unicidad es una clave primaria legible** en vez de tres índices únicos
  parciales.
- **No se puede escribir la consulta mal.** Olvidar `AND role='centroid'` devolvería
  centroides mezclados con ejemplares, con un número plausible y ningún error.

**Lo que decidió:** las operaciones frecuentes (guardar el vector de un item, puntuar
contra carpetas) son específicas de un tipo; la transversal (reindexar) es rarísima. Y la
ventaja de la tabla única para reindexar se consigue igual con una función que recorra las
tres — al revés no funciona, porque las claves foráneas no se recuperan con código.

## Migraciones

`meta.schema_version` guarda la versión. Mientras el proyecto esté en desarrollo temprano,
la vía soportada para un cambio de esquema es **borrar el índice y reindexar**. Es
aceptable porque el índice es una caché reconstruible: la información que importa está en
los archivos del usuario, no en la base de datos.

## Qué NO se versiona ni se sincroniza

El índice es **local a cada máquina** y está entero en `.gitignore`:

1. **Perfiles distintos** → dimensiones y modelos distintos. Los vectores de una máquina
   son ruido sin sentido para la otra.
2. **Rutas absolutas** que no existen en el otro equipo.
3. **Contenido personal** del usuario, que no debe entrar en un commit jamás.
