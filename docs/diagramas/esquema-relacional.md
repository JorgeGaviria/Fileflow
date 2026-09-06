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
        text subdir_policy "unit, ignore o descend"
        text status "active, disabled o deleted"
    }

    folders {
        int id PK
        int parent_id FK "jerarquia, NULL si es raiz"
        text path UK
        text description "senal en lenguaje natural"
        text organize_by "none, month, type o content"
        bool is_trash "acepta lo que nadie quiso"
        bool auto_move "confianza graduada"
        text status "active, disabled o deleted"
    }

    items {
        int id PK
        text item_type "file o dir"
        int child_count "solo dir"
        text path UK "ruta ACTUAL"
        text name
        text extension
        int size_bytes
        real fs_created_at "creacion"
        real fs_modified_at "modificacion"
        text content_hash "archivo o listado"
        text status "6 estados"
        int folder_id FK "destino, NULL sin clasificar"
        text first_seen_at "Fileflow lo vio"
        text organized_at "Fileflow lo movio"
    }

    embeddings {
        int item_id PK "FK a items"
        text vector_space PK "text o image"
        text model_id PK "nunca se mezclan"
        int dimensions
        blob vector "float32 norma 1"
    }

    folder_vectors {
        int folder_id PK "FK a folders"
        text signal PK "description o centroid"
        text vector_space PK
        text model_id PK
        int dimensions
        blob vector
        int sample_count "alimenta beta"
    }

    exemplars {
        int id PK
        int folder_id FK
        text vector_space
        text model_id
        blob vector
        text source_path
    }

    rules {
        int id PK
        text match_type "extension, glob o regex"
        text pattern
        int folder_id FK
        int priority
    }

    decisions {
        int id PK
        int item_id FK
        text candidates_json "top-5 con scores"
        int proposed_folder_id FK
        real confidence
        real margin "score1 menos score2"
        text decided_by "rule, semantic, llm o fallback"
        text verdict
        int final_folder_id FK
    }

    journal {
        int id PK
        text operation "move, rename, mkdir o trash"
        text source_path
        text dest_path
        int item_id FK
        int decision_id FK
        text state "planned, done, failed o undone"
    }

    meta {
        text key PK
        text value
    }

    folders ||--o{ folders : "contiene"
    folders ||--o{ items : "alberga"
    folders ||--o{ folder_vectors : "se representa con"
    folders ||--o{ exemplars : "aprende de"
    folders ||--o{ rules : "es destino de"
    folders ||--o{ decisions : "es propuesta en"
    items ||--o{ embeddings : "se vectoriza en"
    items ||--o{ decisions : "genera"
    items ||--o{ journal : "se mueve en"
    decisions ||--o{ journal : "ejecuta"
```

> **Fileflow no hace `DELETE` de sus propias entidades.** `items`, `folders` y
> `watched_dirs` se dan de baja con `status`, nunca se borran. Por eso el diagrama no
> tiene relaciones de borrado en cascada como camino normal: las cláusulas `ON DELETE`
> están como red de seguridad, no como mecanismo previsto.

## Cómo leerlo

**`folders` e `items` son los dos centros de gravedad.** Todo lo demás cuelga de uno de los
dos, salvo `journal`, que cuelga de ambos porque registra precisamente el acto de unirlos:
mover un archivo a una carpeta.

**`watched_dirs` y `meta` son islas**, sin ninguna relación. Es correcto y deliberado:
`watched_dirs` dice *de dónde* vienen los archivos y `folders` *a dónde* van. Son conceptos
distintos aunque ambos guarden rutas.

**El `status` sustituye al borrado.** Quitar una carpeta no destruye lo que Fileflow
aprendió de ella: al volver a añadirla se reactiva la misma fila y recupera su centroide,
sus ejemplares y su historial de decisiones. Hay además un motivo técnico — SQLite
reutiliza los identificadores de las filas borradas, así que con borrado duro una carpeta
nueva podría heredar los vectores de una anterior sin que nada fallara.

**Las claves primarias compuestas cuentan la historia importante.** En `embeddings` la clave
es `(item_id, vector_space, model_id)`, no solo `item_id`. Eso permite que un mismo archivo tenga a
la vez su vector de `e5-small` y el de `bge-m3` sin pisarse, que es lo que hace posible
cambiar de perfil de hardware sin borrar el índice entero. Lo mismo en `folder_vectors`,
donde `signal` separa la descripción del centroide: las dos señales del scoring.
