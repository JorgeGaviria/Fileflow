# Motor de decisión

El corazón de Fileflow. Recibe un archivo ya vectorizado y devuelve un **ranking de
carpetas candidatas con su puntuación**. No mueve nada: solo opina.

## El problema real

No es "generar" nada. Es responder: *¿a cuál de estas carpetas se parece más este archivo?*
Eso es un problema de **similitud**, y se resuelve comparando vectores.

> Esto matiza la idea inicial de "RAG". Un RAG recupera contexto para que un modelo genere
> texto. Aquí no queremos texto: queremos una decisión. La parte de recuperación se queda,
> la de generación sobra en el camino habitual.

## Las dos señales

Cada carpeta se representa con **dos** vectores, y ninguno de los dos basta por sí solo:

**Descripción** — el embedding de lo que el usuario escribió (*"aquí van las fotos de mis
gatos"*). Existe desde el segundo cero, pero es una intención, no una evidencia. Puede
estar mal redactada o ser demasiado vaga.

**Centroide** — la media de los embeddings de los archivos que ya están dentro. Es evidencia
real de lo que la carpeta contiene, pero **no existe cuando la carpeta está vacía** y es
ruidoso con pocos archivos.

Se complementan exactamente donde el otro falla.

## La fórmula

```
score(archivo, carpeta) = α · sim(v_archivo, v_descripción)
                        + β · sim(v_archivo, v_centroide)
```

con `sim` = similitud coseno, y los pesos dependiendo de cuántos archivos hay en la carpeta:

```
β = n / (n + k)          α = 1 − β
```

donde `n` = número de archivos indexados en la carpeta y `k` una constante de suavizado
(valor inicial propuesto: **k = 10**).

Comportamiento:

| Archivos en la carpeta | α (descripción) | β (contenido) |
|---|---|---|
| 0 | 1.00 | 0.00 |
| 5 | 0.67 | 0.33 |
| 10 | 0.50 | 0.50 |
| 50 | 0.17 | 0.83 |
| 200 | 0.05 | 0.95 |

**Esto es exactamente el "aprendizaje" del que hablaba el planteamiento original.** No se
entrena ningún modelo: la carpeta se define cada vez mejor a sí misma según se llena, y el
peso se desplaza solo. Con la carpeta vacía manda tu descripción; con la carpeta llena
manda la realidad.

### Por qué un solo centroide no siempre basta

Una carpeta como `/documentos` puede contener cosas muy dispares. Su centroide acaba siendo
la media de todo y no se parece a nada en concreto.

Mitigación prevista: si la dispersión interna de una carpeta supera un umbral, se calculan
**varios centroides** (k-means con k pequeño, 2-4) y se puntúa contra el más cercano. Queda
para después de tener métricas; no entra en v1.

## Corrección por ejemplares

Cuando el usuario corrige una propuesta, ese par `(vector del archivo → carpeta correcta)`
se guarda como **ejemplar**. Los ejemplares se puntúan aparte:

```
score_ejemplares(archivo, carpeta) = max sim(v_archivo, v_ejemplar)
                                     sobre los ejemplares de esa carpeta
```

y entran en el score final con su propio peso `γ`. Se usa el **máximo** y no la media a
propósito: un ejemplar es un caso concreto que el usuario corrigió a mano, y basta con
parecerse mucho a *uno* de ellos.

Es la señal más valiosa del sistema porque es supervisión humana directa, y por eso pesa
más por muestra que el centroide.

## Del score a la acción

```
                  mejor score
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ≥ τ_alto      τ_bajo..τ_alto     < τ_bajo
        │              │              │
   Propuesta      Propuesta con    Sin match:
   con confianza  alternativas      /Unsorted
```

Además hay una **regla de margen**: si el primer candidato no supera al segundo por al
menos `δ`, la decisión se marca ambigua aunque el score absoluto sea alto. Dos carpetas
igual de buenas es una señal de duda, no de acierto.

Valores iniciales (a calibrar contra el corpus de pruebas, no son sagrados):
`τ_alto = 0.75`, `τ_bajo = 0.45`, `δ = 0.08`.

## Registro de decisiones

De cada decisión se guarda el **top-5 completo con sus scores**, no solo la elegida. Es
imprescindible para distinguir dos fallos que se parecen pero no tienen nada que ver:

| Síntoma | Diagnóstico | Se arregla con |
|---|---|---|
| La carpeta correcta era la **#2 o #3** | Problema de **calibración** | Ajustar umbrales y pesos |
| La correcta **no estaba en el top-5** | Problema de **representación** | Mejor modelo, mejor extracción o mejor descripción |

Sin ese registro solo sabes que fallaste, no por qué. Con él, cada corrección del usuario
es una muestra etiquetada para medir.

## Métricas

Contra el corpus de `fixtures/`:

- **Precisión top-1** — cuántas veces la propuesta era la correcta
- **Recall top-5** — cuántas veces la correcta estaba entre las cinco
- **Tasa de abstención** — cuántas van a `/Unsorted` (abstenerse es correcto si la
  alternativa era equivocarse)
- **Coste** — ms por archivo y pico de RAM, medido en la máquina de 8 GB

La comparación entre el perfil ligero y el potente sobre estas métricas es la que decide
si el perfil ligero es aceptable. Ver [modelos y perfiles](modelos-y-perfiles.md).
