# Documentación de Fileflow

## Diseño — el porqué de cada decisión

| Documento | Contenido |
|---|---|
| [Arquitectura](diseno/arquitectura.md) | El pipeline en cascada, las capas del código y el ciclo de vida de un archivo |
| [Motor de decisión](diseno/motor-de-decision.md) | La fórmula de scoring, el arranque en frío y el aprendizaje por corrección |
| [Modelos y perfiles](diseno/modelos-y-perfiles.md) | Por qué ONNX y no PyTorch ni TensorFlow, qué modelos y los tres perfiles de hardware |

## Referencia — cómo está construido

| Documento | Contenido |
|---|---|
| [Esquema de datos](referencia/esquema-de-datos.md) | Las tablas de SQLite, con mediciones que justifican no usar base vectorial |
| [Watcher y seguridad](referencia/watcher-y-seguridad.md) | Detección de archivos, journal reversible y casos límite del sistema de archivos |

## Diagramas

| Diagrama | Contenido |
|---|---|
| [Esquema relacional](diagramas/esquema-relacional.md) | Diagrama entidad-relación del índice |

## Proyecto — cómo se trabaja

| Documento | Contenido |
|---|---|
| [Roadmap](proyecto/roadmap.md) | Alcance de v1, qué queda fuera y qué viene después |
| [Entorno de desarrollo](proyecto/entorno-de-desarrollo.md) | Puesta en marcha, trabajo en dos máquinas y corpus de pruebas |

---

## Criterio de organización

Las carpetas separan **por qué se lee un documento**, no por tema:

- **`diseno/`** responde *por qué se decidió así*. Se lee para entender o discutir una
  decisión, y cambia cuando cambia el planteamiento.
- **`referencia/`** responde *cómo funciona lo que hay*. Se consulta mientras se programa,
  y debe mantenerse sincronizado con el código.
- **`diagramas/`** son representaciones visuales. Separadas porque un diagrama se consulta
  solo, sin leer el documento que lo contiene, y porque tienen sus propias herramientas.
- **`proyecto/`** es el proceso, no el producto. No habla de cómo funciona Fileflow sino de
  cómo se construye.

Si un documento nuevo no encaja claramente en una, probablemente esté mezclando dos cosas.
