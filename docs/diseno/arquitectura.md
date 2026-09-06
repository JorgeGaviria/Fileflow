# Arquitectura

## Principio rector

**Lo barato primero, lo caro solo cuando hace falta.**

La mayoría de archivos que llegan a una carpeta de descargas son triviales de clasificar:
un `.exe` es un instalador, un `.zip` es un comprimido, `factura_enero.pdf` lleva la
respuesta en el nombre. Gastar un modelo de lenguaje en eso es tirar tiempo y batería.

Por eso el sistema es una **cascada**: cada etapa es más cara que la anterior y solo
recibe lo que la anterior no supo resolver. En la práctica esperamos que la etapa 1
absorba la gran mayoría del volumen.

## Las cuatro etapas

### Etapa 1 — Reglas rápidas

Coincidencias deterministas sobre metadatos, sin leer el contenido:

- extensión (`.iso` → `/software`)
- patrón en el nombre (`factura_*`, `IMG_*`, `Screenshot *`)
- glob sobre la ruta de origen
- reglas que el propio usuario ha creado desde la bandeja de confirmación

Coste: microsegundos. Si hay match con prioridad suficiente, se acabó.

### Etapa 2 — Extracción de características

Solo para lo que sobrevive a la etapa 1. Convierte el item en algo comparable:

| Tipo | Qué se extrae |
|---|---|
| Texto plano, código, markdown | primeros ~1000 tokens |
| PDF | texto de las primeras páginas; si no hay capa de texto, OCR (opcional) |
| Office (docx, xlsx, pptx) | texto plano de los primeros bloques |
| Imagen | embedding visual directo (no se genera descripción) |
| Audio / vídeo | metadatos y nombre; contenido queda fuera de v1 |
| Binario / desconocido | solo nombre y metadatos |
| **Carpeta como unidad** | su nombre y el listado de lo que contiene |

El resultado es siempre un **vector**. A partir de aquí el sistema no distingue entre un
PDF, una foto y una carpeta entera: son puntos en un espacio.

La receta usada se guarda junto al vector (`embeddings.extractor`). Un vector caduca por
dos motivos, no uno: si cambia el modelo o si cambia la forma de extraer el contenido.

### Etapa 3 — Scoring semántico

Se compara el vector del archivo contra cada carpeta candidata y se produce un ranking.
El detalle está en [motor de decisión](motor-de-decision.md).

Coste: milisegundos. Es una multiplicación de matrices, no una inferencia generativa.

### Etapa 4 — Desempate con LLM *(opcional, no en v1)*

Solo se invoca cuando el ranking está empatado o cuando ninguna carpeta pasa el umbral
mínimo. También es quien sugiere carpetas nuevas ("veo 12 archivos que parecen recibos,
¿creo `/documentos/recibos`?").

**Es opcional por diseño.** En una máquina de 8 GB sin GPU no se carga, y la aplicación
tiene que seguir siendo útil. Si el sistema solo funciona bien con LLM, el diseño está mal.

## Capas del código

```
┌──────────────────────────────────────────────┐
│  UI  —  bandeja de confirmación              │
├──────────────────────────────────────────────┤
│  Orquestador  —  pipeline, cola de trabajo   │
├──────────────┬──────────────┬────────────────┤
│  Watcher     │  Motor de    │  Executor      │
│  (detecta)   │  decisión    │  (mueve+log)   │
├──────────────┴──────┬───────┴────────────────┤
│  Índice (SQLite)    │  Embedders (ONNX)      │
└─────────────────────┴────────────────────────┘
```

Reglas de dependencia:

- **Nada por debajo del orquestador conoce la UI.** El núcleo debe ser usable desde la CLI.
- **El motor de decisión no mueve archivos.** Devuelve un ranking; mover es del executor.
  Así se puede evaluar el motor contra el corpus de pruebas sin tocar el disco.
- **Solo el executor escribe en el sistema de archivos**, y siempre pasando por el journal.
  Un único punto por el que puede perderse un archivo es un punto que se puede blindar.
- **Los embedders son intercambiables** detrás de una interfaz común. El perfil de hardware
  decide cuál se instancia; el resto del código no se entera.

## Ciclo de vida de un archivo

```
detectado ──> estable ──> propuesto ──> confirmado ──> movido
    │            │            │
    │            │            └──> corregido ──> movido (+ ejemplar, si no es excepción)
    │            └──> descartado (temporal, oculto, ignorado)
    └──> re-detectado si cambia antes de estabilizarse
```

Ni la carpeta ni el archivo se borran nunca de la base: se dan de baja con `status`. Ver
[esquema de datos](../referencia/esquema-de-datos.md).

Cada transición queda registrada. El estado vive en la tabla `items`; el histórico de
movimientos, en `journal`; el histórico de decisiones y veredictos, en `decisions`.
