# Diagrama relacional

Diagrama entidad-relación del índice de Fileflow. El DDL que lo implementa está en
[`fileflow/db/schema.sql`](../../fileflow/db/schema.sql) y el razonamiento detrás de cada
tabla, en [esquema de datos](../referencia/esquema-de-datos.md).

> GitHub renderiza este diagrama automáticamente. En VS Code hace falta una extensión de
> Mermaid para verlo dibujado; sin ella se muestra como código.

```mermaid
erDiagram
    watched_dirs {
        int id PK
        text path UK "de donde salen los archivos"
        bool recursive
        bool enabled
    }

    folders {
        int id PK
        text path UK
        text description "senal en lenguaje natural"
        bool auto_move "confianza graduada"
        bool is_trash "carpeta de descarte"
        bool enabled
    }

    files {
        int id PK
        text path UK "ruta ACTUAL"
        text name
        text ext
        int size
        real mtime
        text content_hash "blake2b parcial"
        text status "maquina de estados"
        int folder_id FK
    }

    embeddings {
        int file_id PK "FK a files"
        text kind PK "text o image"
        text model_id PK "nunca se mezclan"
        int dim
        blob vector "float32 norma 1"
    }

    folder_vectors {
        int folder_id PK "FK a folders"
        text source PK "description o centroid"
        text kind PK
        text model_id PK
        int dim
        blob vector
        int n_samples "alimenta beta"
    }

    exemplars {
        int id PK
        int folder_id FK
        text kind
        text model_id
        blob vector
    }

    rules {
        int id PK
        text kind "ext, glob o regex"
        text pattern
        int folder_id FK
        int priority
    }

    decisions {
        int id PK
        int file_id FK
        text candidates "JSON top-5 con scores"
        int proposed_folder_id FK
        real confidence
        real margin "score1 menos score2"
        text stage
        text verdict
        int final_folder_id FK
    }

    journal {
        int id PK
        text op "move, rename, mkdir o trash"
        text src
        text dst
        int file_id FK
        int decision_id FK
        text state "planned, done, failed o undone"
    }

    meta {
        text key PK
        text value
    }

    folders ||--o{ files : "alberga"
    folders ||--o{ folder_vectors : "se representa con"
    folders ||--o{ exemplars : "aprende de"
    folders ||--o{ rules : "es destino de"
    folders ||--o{ decisions : "es propuesta en"
    files ||--o{ embeddings : "se vectoriza en"
    files ||--o{ decisions : "genera"
    files ||--o{ journal : "se mueve en"
    decisions ||--o{ journal : "ejecuta"
```

## Cómo leerlo

**`folders` y `files` son los dos centros de gravedad.** Todo lo demás cuelga de uno de los
dos, salvo `journal`, que cuelga de ambos porque registra precisamente el acto de unirlos:
mover un archivo a una carpeta.

**`watched_dirs` y `meta` son islas**, sin ninguna relación. Es correcto y deliberado:
`watched_dirs` dice *de dónde* vienen los archivos y `folders` *a dónde* van. Son conceptos
distintos aunque ambos guarden rutas.

**Las claves primarias compuestas cuentan la historia importante.** En `embeddings` la clave
es `(file_id, kind, model_id)`, no solo `file_id`. Eso permite que un mismo archivo tenga a
la vez su vector de `e5-small` y el de `bge-m3` sin pisarse, que es lo que hace posible
cambiar de perfil de hardware sin borrar el índice entero. Lo mismo en `folder_vectors`,
donde `source` separa la descripción del centroide: las dos señales del scoring.
