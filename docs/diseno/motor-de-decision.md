# Motor de decisión

El corazón de Fileflow. Recibe un item ya vectorizado y devuelve un **ranking de carpetas
candidatas con su puntuación**. No mueve nada: solo opina.

## El problema real

No es "generar" nada. Es responder: *¿a cuál de estas carpetas se parece más esto?* Eso es
un problema de **similitud**, y se resuelve comparando vectores.

> Esto matiza la idea inicial de "RAG". Un RAG recupera contexto para que un modelo genere
> texto. Aquí no queremos texto: queremos una decisión. La parte de recuperación se queda,
> la de generación sobra en el camino habitual.

## Las dos señales

Cada carpeta se representa con **dos** vectores, y ninguno basta por sí solo:

**Descripción** — el embedding de lo que escribió el usuario (*"aquí van las fotos de mis
gatos"*). Existe desde el segundo cero, pero es una intención, no una evidencia.

**Centroide** — la media de los embeddings de lo que ya está dentro. Es evidencia real,
pero **no existe cuando la carpeta está vacía** y es ruidoso con pocos elementos.

Se complementan exactamente donde el otro falla.

## La fórmula

```
score(item, carpeta) = α · sim(v_item, v_descripción)
                     + β · sim(v_item, v_centroide)
                     + γ · max sim(v_item, ejemplares positivos)
```

con `sim` = similitud coseno, y los pesos de las dos primeras dependiendo de cuánto
contiene la carpeta:

```
β = n / (n + k)          α = 1 − β
```

donde `n` = `sample_count` del centroide y `k` una constante de suavizado (valor inicial
propuesto: **k = 10**).

| Elementos en la carpeta | α (descripción) | β (contenido) |
|---|---|---|
| 0 | 1.00 | 0.00 |
| 10 | 0.50 | 0.50 |
| 50 | 0.17 | 0.83 |
| 200 | 0.05 | 0.95 |

**Esto es el "aprendizaje" del planteamiento original.** No se entrena ningún modelo: la
carpeta se define cada vez mejor a sí misma según se llena, y el peso se desplaza solo.

## El centroide contiene solo lo que aterrizó ahí

Una carpeta madre **no hereda** el contenido de sus hijas. Esto parece contraintuitivo y es
importante, así que conviene ver por qué.

Imagina `/imagenes` con una sola hija, `/imagenes/gatos`, y que todo lo que ha llegado son
fotos de gatos. Si propagáramos hacia arriba, el centroide de `/imagenes` **también sería
de gatos**. Entonces:

```
Llega una foto de un paisaje

/imagenes/gatos   0.30 ✗   no se parece a un gato
/imagenes         0.30 ✗   ...porque su centroide TAMBIÉN es de gatos
→ ninguna cualifica → /Unsorted
```

Y ese paisaje debería ir a `/imagenes` sin ninguna duda.

**El problema de fondo:** una carpeta madre tiene que ser *más general* que sus hijas, pero
un centroide construido con el contenido de las hijas es exactamente igual de específico
que ellas. La propagación convierte a la madre en una copia de su hija y le quita la
capacidad de hacer su verdadero trabajo: **recoger lo que no encaja en ninguna hija**.

Con una sola hija es descarado; con varias se disimula, pero el sesgo sigue ahí.

Y el diseño ya resuelve bien el caso sin propagar: una madre sin elementos propios tiene
`n = 0`, luego `β = 0`, y **manda su descripción al 100 %**. Que es lo honesto: si nunca ha
aterrizado nada directamente ahí, no tenemos evidencia y hay que fiarse de lo que escribió
el usuario.

> **Consecuencia para la interfaz:** la descripción de las carpetas madre importa mucho.
> *"aquí van las imágenes agrupadas en carpetas"* describe la organización, no el
> contenido, y compite mal. *"fotos, capturas, dibujos, imágenes de cualquier tipo"*
> funciona mucho mejor. La interfaz debería empujar al usuario hacia lo segundo.

## Precedencia: gana la hija cualificada

Cuando un item encaja en una carpeta y en su madre:

> **Gana el candidato más profundo de entre los que superan el umbral.** No el que más
> puntúe.

```
Foto de gato       /imagenes 0.88 ✓   /imagenes/gatos 0.80 ✓
                   ambos cualifican → gana la HIJA

Foto de paisaje    /imagenes 0.80 ✓   /imagenes/gatos 0.30 ✗
                   solo cualifica la madre → gana la MADRE
```

La hija **no necesita ganar en puntuación, solo cualificar**. Si el usuario creó
`/imagenes/gatos` es porque quiere las fotos de gatos ahí.

## Del score a la acción

```
                  mejor score
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ≥ τ_alto      τ_bajo..τ_alto     < τ_bajo
        │              │              │
   Propuesta      Propuesta con    Ninguna cualifica:
   con confianza  alternativas     se puntúa entre papeleras
```

Hay además una **regla de margen**: si el primer candidato no supera al segundo por al
menos `δ`, la decisión se marca ambigua aunque el score absoluto sea alto. Dos carpetas
igual de buenas son señal de duda, no de acierto.

Valores iniciales, a calibrar contra el corpus de pruebas: `τ_alto = 0.75`,
`τ_bajo = 0.45`, `δ = 0.08`.

**Las papeleras no compiten con las carpetas normales.** Solo si ninguna carpeta normal
supera el umbral se puntúa entre las carpetas con `is_trash`, y ahí gana la mejor **sin
umbral**: alguien tiene que quedarse el archivo. Puede haber varias papeleras, y su
descripción sirve para repartir entre ellas.

## Las correcciones del usuario

Cada corrección contiene **dos** informaciones:

```
POSITIVA   este archivo SÍ va en /facturas       → ejemplar positivo
NEGATIVA   este archivo NO iba en /contratos     → ejemplar negativo
```

Y **no valen lo mismo**. Un positivo dice "esto se parece a aquello" y generaliza bien. Un
negativo puede ser una **excepción disfrazada de regla**: quizá esa factura fue a
`/impuestos` solo porque el usuario estaba haciendo la declaración, y penalizar `/facturas`
por eso estropearía las siguientes.

Por eso:

- **Los positivos entran en el scoring** con peso `γ`, puntuando por **máxima** similitud y
  no por media. Una corrección es un caso concreto que el usuario resolvió a mano, y basta
  con parecerse mucho a *uno* de ellos.
- **Los negativos se guardan pero no puntúan** en v1. Se usan para calibrar: si
  `/contratos` acumula muchas propuestas rechazadas, lo que hay que subir es **su umbral**,
  no penalizar archivo por archivo. Es la misma información aplicada a nivel de carpeta:
  más estable y más fácil de depurar.
- **El negativo necesita corroboración; el positivo no.** Un solo negativo es probablemente
  una excepción; tres contra la misma carpeta con archivos parecidos ya son un patrón.

### Preguntar en vez de adivinar

En la bandeja de confirmación, al corregir:

```
  ○ Solo este archivo        ← excepción: no aprendas nada
  ● Siempre que se parezca   ← esto sí es una regla
```

Un clic resuelve en el origen lo que si no habría que adivinar con heurísticas, y se
registra en `decisions.is_exception`.

## Métricas: tasa de acuerdo, no precisión

**No existe una verdad objetiva.** Si el usuario mete una factura en `/impuestos/2026`
porque está preparando la declaración, entonces `/facturas` estaba mal *para él*. El
objetivo de Fileflow no es acertar según una taxonomía universal, sino colocar los archivos
donde **esta persona** los quiere.

Por eso lo que medimos no es *precisión* sino **tasa de acuerdo con el usuario**, y las
excepciones quedan fuera del cálculo. Llamarlo por su nombre importa: tratar cada
corrección como un error nuestro nos haría perseguir un objetivo que no existe.

Contra el corpus de `fixtures/`:

- **Acuerdo top-1** — cuántas veces la propuesta se aceptó sin corregir
- **Recall top-5** — cuántas veces la carpeta final estaba entre las cinco candidatas
- **Tasa de abstención** — cuántas fueron a la papelera (abstenerse es correcto si la
  alternativa era equivocarse)
- **Coste** — ms por item y pico de RAM, medido en la máquina de 8 GB

La comparación entre el perfil ligero y el potente sobre estas métricas es la que decide si
el perfil ligero es aceptable. Ver [modelos y perfiles](modelos-y-perfiles.md).

## Registro de decisiones

De cada decisión se guarda el **top-5 completo con sus scores**, no solo la elegida. Es lo
que distingue dos fallos que se parecen pero no tienen nada que ver:

| Síntoma | Diagnóstico | Se arregla con |
|---|---|---|
| La correcta era la **#2 o #3** | **Calibración** | Ajustar umbrales y pesos |
| La correcta **no estaba en el top-5** | **Representación** | Mejor modelo, extracción o descripción |

Sin ese registro solo sabes que fallaste, no por qué.

## Pendiente

**Carpetas heterogéneas.** Una carpeta como `/documentos` puede contener cosas muy
dispares, y su centroide acaba siendo la media de todo, sin parecerse a nada en concreto.
Mitigación prevista: si la dispersión interna supera un umbral, calcular **varios
centroides** (k-means con k pequeño) y puntuar contra el más cercano. Queda para después de
tener métricas.
