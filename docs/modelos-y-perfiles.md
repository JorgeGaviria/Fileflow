# Modelos y perfiles de hardware

## El stack, por capas

Una confusión frecuente es comparar herramientas que no compiten entre sí. Conviene tener
claras las capas:

| Capa | Qué hace | Opciones |
|---|---|---|
| **Runtime** | Ejecuta las operaciones matemáticas | PyTorch, TensorFlow, **ONNX Runtime**, llama.cpp |
| **Librería de modelos** | API de alto nivel sobre el runtime | sentence-transformers, transformers, open-clip, fastembed |
| **Pesos** | El modelo entrenado concreto | multilingual-e5, bge-m3, CLIP, SigLIP |

TensorFlow compite con **PyTorch**, no con sentence-transformers (que de hecho corre
*sobre* PyTorch).

## Por qué no TensorFlow

**Porque no vamos a entrenar nada.** PyTorch y TensorFlow son frameworks de entrenamiento;
nosotros solo hacemos inferencia con modelos ya entrenados. Toda su flexibilidad para
definir y derivar grafos es peso muerto en este proyecto.

Y hay una razón práctica más fuerte: **el ecosistema de embeddings pre-entrenados es
PyTorch-first**. Los modelos competitivos (la familia bge, e5, gte) se publican en PyTorch;
los ports a TensorFlow son escasos y suelen estar desactualizados. Elegir TF significaría
renunciar a los buenos modelos o portarlos a mano.

Comparación concreta, el mismo trabajo:

```python
# sentence-transformers
model = SentenceTransformer("intfloat/multilingual-e5-small")
emb = model.encode(["texto del archivo"])     # ndarray normalizado, listo
```

Con TensorFlow puro habría que cargar el tokenizer, construir el grafo, y gestionar a mano
el padding, la máscara de atención y el pooling.

## Por qué ONNX Runtime para distribuir

Aquí sí hay una decisión real, y no es entre PyTorch y TF:

| | PyTorch + sentence-transformers | ONNX Runtime + fastembed |
|---|---|---|
| Tamaño instalado | ~2-3 GB | ~150-200 MB |
| Velocidad en CPU | buena | **igual o mejor** |
| Cuantización int8 | requiere trabajo extra | integrada |
| Catálogo de modelos | completo | los principales |
| Empaquetar la app | doloroso | manejable |

Para una aplicación de escritorio que hay que instalar, arrastrar 3 GB de PyTorch es
inaceptable. Y en CPU —que es el caso de la mayoría de usuarios— ONNX Runtime rinde igual
o mejor porque está optimizado justo para eso.

**Decisión: desarrollar con sentence-transformers, distribuir con ONNX Runtime.**

Ambos quedan detrás de la misma interfaz `Embedder`, así que se puede experimentar con
comodidad y exportar a ONNX cuando el modelo esté decidido. El resto del código no se entera.

## Perfiles

|  | **Ligero** | **Equilibrado** | **Potente** |
|---|---|---|---|
| Objetivo | Cualquier PC, sin GPU | 16 GB RAM | GPU ≥ 8 GB VRAM |
| Texto | multilingual-e5-small (384d) | multilingual-e5-base (768d) | bge-m3 (1024d) |
| Imagen | CLIP ViT-B/32 (512d) | SigLIP base | SigLIP large |
| LLM desempate | ninguno | Qwen2.5 3B (Ollama) | Llama 3.1 8B |
| Runtime | ONNX int8, CPU | ONNX, CPU | PyTorch, CUDA |
| RAM aprox. | ~350 MB | ~2 GB | GPU |

Los modelos de texto son **multilingües a propósito**: la interfaz y las descripciones de
carpetas están en español, y los modelos solo-inglés (como los `bge-*-en`) degradan mucho
ahí. Es un requisito, no una preferencia.

### Selección automática

En el primer arranque se ejecuta un **benchmark corto** (~10 s) que mide RAM disponible,
núcleos, presencia de CUDA y throughput real de embeddings, y preselecciona un perfil.
El usuario siempre puede cambiarlo a mano.

Es mejor medir que deducir: dos máquinas con la misma RAM sobre el papel pueden rendir muy
distinto según lo que ya tengan cargado.

## El detalle que hay que respetar: dimensiones incompatibles

**Los embeddings de modelos distintos no son comparables.** No es solo que las dimensiones
no cuadren (384 vs 768 vs 1024): es que aunque cuadraran, cada modelo organiza su espacio
vectorial a su manera. Comparar un vector de e5-small con uno de bge-m3 no da un resultado
malo, da un resultado **sin sentido**.

Consecuencia directa sobre el diseño:

1. Cada vector se guarda con su `model_id` y su `dim`. Siempre.
2. Toda consulta de similitud filtra por `model_id`. Nunca se mezclan.
3. Cambiar de perfil **invalida el índice** y obliga a re-indexar.

El re-indexado es lento pero **no destructivo**: los archivos no se tocan, solo se
recalculan vectores. Corre en segundo plano y la aplicación sigue usable con reglas rápidas
mientras tanto.

Esto importa especialmente en este proyecto porque se desarrolla en dos máquinas con
perfiles distintos: cada una tendrá su propio índice, y por eso la base de datos **no se
versiona ni se sincroniza**. Ver [entorno de desarrollo](entorno-de-desarrollo.md).

## Sobre el LLM

Queda fuera de v1 y es opcional para siempre. Razones:

- En la máquina de 8 GB no cabe con dignidad: un 3B en Q4 son ~2,5 GB y en CPU va lento.
- Un loop agéntico por archivo no escala: 40 archivos de una descarga serían minutos.
- Si el sistema **necesita** el LLM para acertar, el motor de similitud está mal calibrado
  y eso es lo que hay que arreglar.

Su sitio natural, cuando llegue, son las tareas donde los embeddings no llegan:

- desempatar candidatos con scores casi idénticos
- **sugerir carpetas nuevas** a partir de patrones en `/Unsorted`
- proponer una descripción para una carpeta que el usuario dejó sin describir
