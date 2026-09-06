# Roadmap

## v1 — El núcleo utilizable

El objetivo de v1 no es impresionar, es **ser medible**: una aplicación que propone, se
puede corregir y registra lo suficiente para saber si acierta.

Restricción de diseño: **v1 tiene que funcionar bien en la máquina de 8 GB sin GPU.**
Sin LLM, sin GPU, sin dependencias pesadas.

| # | Hito | Estado |
|---|---|---|
| 1 | Repositorio, esquema SQLite, capa de índice | ✅ |
| 2 | Watcher: debounce, estabilidad, reconciliación | ✅ |
| 3 | Motor de reglas rápidas (extensión, glob, regex) | ⬜ |
| 4 | Extracción de texto (txt, md, pdf, docx) | ⬜ |
| 5 | Embeddings ONNX: e5-small y CLIP ViT-B/32 | ⬜ |
| 6 | Scoring: descripción + centroide + ejemplares | ⬜ |
| 7 | Executor + journal + undo | ⬜ |
| 8 | Bandeja de confirmación (CLI primero, luego UI) | ⬜ |
| 9 | Corpus de pruebas y métricas | ⬜ |

### Qué queda explícitamente fuera de v1

- **Movimiento automático.** Todo pasa por confirmación. Sin datos de precisión, activar el
  automático es apostar con los archivos del usuario.
- **LLM.** Ver [modelos y perfiles](modelos-y-perfiles.md).
- **Explorador de archivos.** Ver más abajo.
- **OCR**, audio y vídeo.

### Definición de "terminado"

v1 está lista cuando, sobre el corpus de `fixtures/` y en la máquina de 8 GB:

- precisión top-1 **≥ 80 %** en carpetas con 20+ archivos
- recall top-5 **≥ 95 %**
- **< 500 ms** por archivo de texto, extracción incluida
- **cero** archivos perdidos en 1000 movimientos con undo verificado

El último criterio no es negociable; los otros tres son objetivos.

---

## v2 — Donde la cosa se pone interesante

### Búsqueda semántica

**La mejor relación valor/esfuerzo de todo el proyecto.** Los archivos ya están
vectorizados por el core: buscar por significado es reutilizar ese índice.

```
> la factura de la luz de marzo
> el pdf del contrato del carro
> fotos de la playa del año pasado
```

Encuentra por *contenido*, no por nombre. Es la función que hace que alguien recomiende la
aplicación, y sale casi de regalo.

### Confianza graduada → automático

Cuando una carpeta acumula suficientes decisiones con precisión alta (propuesta inicial:
**≥ 20 decisiones con ≥ 95 % de acierto**), la aplicación ofrece activar el automático
**solo para esa carpeta**. Con el journal y el undo intactos.

La confianza se gana con evidencia registrada, no se asume ni se configura a ciegas.

### LLM opcional

Desempate de candidatos empatados, sugerencia de carpetas nuevas a partir de patrones en
`/Unsorted`, y redacción de descripciones para carpetas sin describir. Vía Ollama, opcional,
detectado en tiempo de ejecución.

### Organización de `/Unsorted`

Lo que pide el planteamiento original: subdividir la carpeta de descarte por **mes**, por
**tipo** o por **agrupación semántica** (clustering de los embeddings que ya existen).

### Otras

- Centroides múltiples para carpetas heterogéneas
- Reglas de nombrado (renombrar al mover según plantilla)
- Detección de duplicados en todo el árbol

---

## v3 — El explorador

El *"explorador de archivos con esteroides"* del planteamiento original.

**Va aquí a propósito.** Un explorador de archivos es un proyecto entero por sí mismo:
árbol, vista de lista, previsualizaciones, arrastrar y soltar, menús contextuales, atajos,
selección múltiple, rendimiento con miles de elementos. Puesto en v1 se comería el tiempo
que necesita el núcleo, y el resultado sería un explorador mediocre que además organiza mal.

Además hay un orden natural: la búsqueda semántica de v2 **es** la funcionalidad que
justificaría el explorador. Construir primero el motor y luego la interfaz es el orden
correcto, no el inverso.

Ideas para cuando llegue: navegación por similitud ("archivos parecidos a este"), vistas
virtuales por consulta semántica, línea de tiempo, y organizar arrastrando (cada arrastre
es un ejemplar nuevo).

---

## Fuera de alcance, por ahora

- **Sincronización en la nube.** El proyecto es local por diseño.
- **Multiusuario / servidor.**
- **Móvil.**
- **Linux y macOS.** El núcleo se escribe en Python multiplataforma y se evita atarlo a
  Windows sin necesidad, pero solo se prueba en Windows.
