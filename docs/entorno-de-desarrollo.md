# Entorno de desarrollo

## Puesta en marcha

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

python -m fileflow init
python -m fileflow watch "C:\Users\<usuario>\Downloads"
```

Requiere **Python 3.12+**.

## Las dos máquinas

El proyecto se desarrolla en dos equipos con perfiles muy distintos, y eso es una ventaja
si se aprovecha bien.

| | **Maria** (portátil) | **Torre** |
|---|---|---|
| CPU | i5-12450H (8 núcleos) | Ryzen 5 3600 (6c/12h) |
| RAM | 8 GB | 32 GB |
| GPU | Intel UHD (integrada) | RTX 3070, 8 GB VRAM |
| Perfil | **Ligero** | **Potente** |
| Runtime | ONNX Runtime, CPU, int8 | PyTorch + CUDA |
| LLM | ninguno | Ollama, 7-8B |

### Regla de trabajo

> **Se desarrolla en Maria. Se compara en la Torre.**

**Maria es la máquina de referencia.** Con 8 GB, Windows se lleva 3-4 GB y el editor con el
navegador otros 2-3: el presupuesto real de la aplicación es **~1,5 GB**. Es una
restricción incómoda y por eso es útil — obliga a que la aplicación sea genuinamente
ligera, que es la condición en la que está la mayoría de la gente. Si funciona bien aquí,
funciona en cualquier sitio.

**La Torre no es para desarrollar cómodo, es el patrón de calidad.** Ejecuta el corpus de
pruebas con el perfil potente y da el techo de precisión alcanzable. La diferencia entre
ambas cifras es el dato que de verdad importa:

- Si el perfil ligero queda **a menos de 5 puntos** del potente → e5-small es suficiente y
  el LLM es un lujo.
- Si la brecha es **grande** → hay que mejorar la extracción o subir de modelo, y conviene
  saberlo pronto.

Sin esa comparación no hay forma de saber si el perfil ligero es aceptable o si se está
entregando una versión degradada sin darse cuenta.

## Qué se sincroniza y qué no

Solo se versiona el **código, la documentación y las fixtures**. Nada más.

| | Versionado |
|---|---|
| Código, docs, `fixtures/` | ✅ |
| `.fileflow/` (índice, embeddings) | ❌ |
| `models/` (pesos descargados) | ❌ |
| `config.local.json` | ❌ |
| `.venv/` | ❌ |

Los tres motivos por los que el índice **no** puede compartirse entre las dos máquinas:

1. **Perfiles distintos** → dimensiones y modelos distintos. Los vectores de una máquina
   son ruido sin sentido para la otra.
2. **Rutas absolutas** que no existen en el otro equipo.
3. **Contenido personal** del usuario, que no debe entrar en un commit jamás.

Cada máquina construye su propio índice, y por eso cada vector guarda su `model_id`
(ver [esquema de datos](esquema-de-datos.md)).

## El corpus de pruebas (`fixtures/`)

Sin corpus no hay forma de saber si un cambio mejora o empeora, ni de comparar las dos
máquinas. Es la pieza que convierte "parece que va mejor" en un número.

```
fixtures/
├── manifest.json      # ruta -> carpeta correcta (etiquetado a mano)
├── docs/              # facturas, contratos, recibos, apuntes
├── images/            # fotos, capturas, memes, diagramas
├── media/             # audio y video (nombres, no contenido)
└── misc/              # instaladores, zips, torrents, sin extension
```

Reglas:

- **~50 archivos**, pequeños (< 100 KB cada uno) para que quepan en el repositorio.
- **Nada personal ni con datos reales.** Contenido inventado o anonimizado.
- **Casos difíciles a propósito**: nombres ambiguos (`documento (1).pdf`), archivos sin
  extensión, un PDF escaneado sin capa de texto, dos archivos casi idénticos que van a
  carpetas distintas.
- `manifest.json` es la verdad de referencia, etiquetada a mano.

Los casos fáciles no informan de nada: si el 100 % del corpus se resuelve con reglas de
extensión, el corpus no está midiendo el motor semántico.

```bash
python -m fileflow eval fixtures/     # precision top-1, recall top-5, ms/archivo
```

## Git

El repositorio tiene identidad **local** propia:

```bash
git config --local user.name  "jogaviriab"
git config --local user.email "jorgegaviria310@gmail.com"
```

Es local (`--local`, no `--global`) a propósito: la identidad global del sistema es otra
(`jogaviriab@unal.edu.co`) y se deja intacta para no afectar a los demás proyectos.
Los commits de Fileflow salen con la cuenta de gmail sin tener que acordarse de nada.

Comprobar en cualquier momento con:

```bash
git config --local --list | grep user
```

## Dependencias

Mínimas por principio: cada dependencia es tamaño de instalación y una cosa más que puede
romperse al empaquetar.

| Paquete | Para qué | Cuándo |
|---|---|---|
| `watchdog` | Vigilancia del sistema de archivos | v1 |
| `numpy` | Similitud coseno, centroides | v1 |
| `onnxruntime` | Inferencia de embeddings | v1 |
| `pypdf`, `python-docx` | Extracción de texto | v1 |
| `Pillow` | Carga de imágenes | v1 |
| `sentence-transformers` | Experimentación (solo dev) | dev |

`sqlite3` viene en la biblioteca estándar. No hace falta base vectorial
(ver [esquema de datos](esquema-de-datos.md)).
