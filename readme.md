# Fileflow

Un organizador de archivos que entiende **de qué trata** cada archivo, no solo su extensión.

Tú describes tus carpetas en lenguaje natural y Fileflow se encarga del resto: vigila los
directorios donde caen los archivos nuevos (Descargas, Documentos, el escritorio) y propone
dónde va cada uno.

```
/imagenes            : Aqui van todas las imagenes, agrupadas en subcarpetas
/imagenes/gatos      : Fotos de mis gatos
/documentos/facturas : Facturas y recibos de servicios
```

Sin escribir una sola regla. Y funciona **100% en local**: ningún archivo sale de tu máquina
y no hay costes de API.

---

## Cómo funciona

```
                Archivo nuevo en /Descargas
                            │
                            ▼
            ┌───────────────────────────────┐
            │   1. Reglas rápidas           │   ~90% de los casos,
            │   (extensión, patrón, glob)   │   sin tocar un modelo
            └───────────────┬───────────────┘
                            │ requiere análisis
                            ▼
            ┌───────────────────────────────┐
            │   2. Extracción               │
            │   Texto → primeros N tokens   │
            │   Imagen → embedding CLIP     │
            └───────────────┬───────────────┘
                            │
                            ▼
            ┌───────────────────────────────┐
            │   3. Scoring semántico        │
            │   descripción de la carpeta   │
            │   + contenido ya archivado    │
            └───────────────┬───────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
    [Confianza alta]              [Ambiguo o sin match]
    Propuesta directa             Bandeja de revisión
                                  o carpeta /Unsorted
```

Cada decisión queda registrada en un **journal reversible**: cualquier movimiento se puede
deshacer, siempre.

## Las dos ideas centrales

**1. La descripción resuelve el arranque en frío.** Una carpeta recién creada no tiene
contenido del que aprender, pero sí tiene tu descripción. Fileflow puntúa cada archivo
contra ambas señales y ajusta el peso automáticamente: al principio manda lo que escribiste,
y a medida que la carpeta se llena manda lo que hay dentro.

**2. Tu corrección es el dato que importa.** Fileflow **propone, tú decides**. Cada vez que
corriges una propuesta, esa corrección se guarda como ejemplo y mejora las siguientes.
No hay reentrenamiento: el sistema mejora porque su memoria mejora.

Cuando una carpeta acumula suficientes aciertos, Fileflow te ofrece activar el modo
automático **solo para esa carpeta**. La confianza se gana con evidencia, no se asume.

## Estado

🚧 **En desarrollo — v1 en construcción.**

| | |
|---|---|
| ✅ | Repositorio, esquema de datos, capa de índice |
| ✅ | Watcher con debounce, detección de estabilidad y reconciliación |
| ⬜ | Motor de reglas rápidas |
| ⬜ | Embeddings ONNX (texto e imagen) |
| ⬜ | Scoring y motor de decisión |
| ⬜ | Journal con undo |
| ⬜ | Bandeja de confirmación (UI) |

## Documentación

| Documento | Contenido |
|---|---|
| [Arquitectura](docs/arquitectura.md) | Pipeline en cascada, capas y flujo de un archivo |
| [Motor de decisión](docs/motor-de-decision.md) | Scoring, arranque en frío, aprendizaje por corrección |
| [Modelos y perfiles](docs/modelos-y-perfiles.md) | Por qué ONNX, qué modelos y perfiles de hardware |
| [Esquema de datos](docs/esquema-de-datos.md) | Tablas de SQLite y decisiones de diseño |
| [Watcher y seguridad](docs/watcher-y-seguridad.md) | Detección de archivos, journal, undo y casos límite |
| [Roadmap](docs/roadmap.md) | Alcance de v1 y qué viene después |
| [Entorno de desarrollo](docs/entorno-de-desarrollo.md) | Puesta en marcha y trabajo en dos máquinas |

## Requisitos

- Python 3.12+
- Windows (por ahora; el núcleo es multiplataforma)
- Sin GPU necesaria — el perfil ligero corre en CPU con ~350 MB de RAM

## Puesta en marcha

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

python -m fileflow init                  # crea el indice
python -m fileflow watch ~/Downloads     # empieza a vigilar
```

Ver [entorno de desarrollo](docs/entorno-de-desarrollo.md) para el detalle.
