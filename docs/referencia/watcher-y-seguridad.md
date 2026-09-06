# Watcher y seguridad

Este documento cubre las dos mitades del contacto con el sistema de archivos: **detectar**
cambios y **ejecutar** movimientos sin perder nada.

> La segunda mitad es la que decide si la aplicación sobrevive. Un organizador que
> clasifica de maravilla pero un día pierde un archivo se desinstala esa misma tarde. La
> precisión es un objetivo; **no perder archivos es un requisito**.

---

# Parte 1 — Detección

## Por qué no basta con escuchar eventos

`watchdog` (sobre `ReadDirectoryChangesW` en Windows) avisa de cambios, pero en crudo es
inservible:

- **Un archivo genera muchos eventos.** Una descarga produce decenas de `modified` mientras
  se escribe.
- **El evento llega antes de que el archivo esté completo.** Reaccionar al primero significa
  leer un archivo a medias, o peor, moverlo mientras el navegador escribe en él.
- **No hay eventos mientras la aplicación está cerrada.** Lo que llegó anoche es invisible.
- Renombrar se ve como `deleted` + `created`, sin relación entre ambos.

Por eso hay tres mecanismos y no uno.

## Mecanismo 1 — Debounce

Los eventos no se procesan: se acumulan en un diccionario `ruta → timestamp del último
evento`. Un hilo trabajador emite la ruta cuando lleva **N segundos sin actividad**
(por defecto 2 s).

Cincuenta eventos de una descarga se colapsan en un solo trabajo.

## Mecanismo 2 — Detección de estabilidad

Silencio no es sinónimo de terminado: una descarga lenta puede pasar 2 segundos sin
escribir. Antes de tocar nada se comprueba que el archivo está **estable**:

1. `size` y `mtime` idénticos en dos muestras separadas por un intervalo
2. el archivo se puede abrir en modo lectura exclusiva (en Windows esto falla si otro
   proceso lo tiene abierto para escritura — es la comprobación más fiable de las tres)
3. la extensión no es de las temporales

Extensiones y patrones descartados de entrada:

```
.crdownload  .part  .partial  .download  .tmp  .temp  .!ut  .opdownload
~$*                  (archivos de bloqueo de Office)
.*                   (ocultos en estilo Unix)
+ atributos oculto y de sistema en Windows
```

Si no está estable, se reprograma con backoff en lugar de descartarse. Una ISO de 8 GB
puede tardar media hora, y eso es normal, no un error.

## Qué se considera una cosa: la política de subcarpetas

Antes de detectar nada hay que decidir qué cuenta como *una* cosa. Lo dice
`watched_dirs.subdir_policy`:

| Valor | Qué hace |
|---|---|
| `unit` | Cada subcarpeta de primer nivel es **una** cosa, se clasifica y se mueve entera |
| `ignore` | No se mira dentro en absoluto |
| `descend` | Cada archivo de dentro se clasifica por separado |

**El defecto es `unit`, y es una decisión de seguridad.** Con `descend`, extraer un `.zip`
en Descargas repartiría `index.html`, `logo.css` y `manual.pdf` por tres carpetas
distintas, deshaciendo una carpeta que el usuario mantiene junta a propósito.

Cuando una carpeta es la unidad, **su contenido no se indexa por separado**: si no, habría
47 decisiones pendientes además de la de la carpeta.

Y una carpeta destino registrada **nunca** se trata como unidad, esté donde esté. Sin esa
regla, `/Descargas/facturas` —vigilada por fuera y destino a la vez— acabaría intentando
moverse dentro de sí misma.

## Mecanismo 3 — Reconciliación al arrancar

Los eventos son efímeros; el estado no. Al iniciar se recorren los directorios vigilados y
se comparan contra la tabla `items`:

| Situación | Acción |
|---|---|
| En disco, no en BD | Alta como `pending` |
| En BD y en disco, `mtime`/`size` distintos | Se marca para reanalizar |
| En BD, no en disco | Se marca `missing` |
| Coincide | Solo se actualiza `last_seen` |

Sin esto la aplicación solo vería lo que ocurre mientras está abierta, que no es como se
usa un ordenador de verdad.

---

# Parte 2 — Ejecución segura

## El journal

**Toda** operación pasa por la tabla `journal`, y siempre en este orden:

```
1. Escribir la intención     journal(op='move', src=..., dst=..., state='planned')
2. Ejecutar en disco
3. Marcar el resultado       state='done'  (o 'failed' + error)
```

Escribir *antes* de actuar es lo que importa. Si la aplicación se cierra a mitad de un
movimiento, al arrancar quedan entradas en `planned` y se sabe exactamente qué operación
quedó a medias y dónde mirar. Con el registro escrito *después*, esa información se pierde
justo cuando hace falta.

## Undo

Cada entrada `done` es reversible:

```bash
python -m fileflow undo --last          # deshacer el ultimo movimiento
python -m fileflow undo --session <id>  # deshacer un lote completo
python -m fileflow undo --since 1h      # deshacer por ventana de tiempo
```

Deshacer un `move` es moverlo de vuelta a `src`. Antes se verifica que el archivo sigue en
`dst` y que `src` está libre; si no, se avisa en vez de forzar.

El undo **está disponible siempre**, también en modo automático. Es precisamente en
automático donde más falta hace.

## Casos límite que hay que respetar

### Colisión de nombres

El destino ya tiene un archivo con ese nombre. Nunca se sobrescribe. Se compara el
`content_hash`:

- **Mismo hash** → es un duplicado real. Se propone descartar el nuevo (a la papelera del
  sistema, no borrado), y se pide confirmación.
- **Hash distinto** → son archivos distintos con el mismo nombre. Se renombra a
  `nombre (2).ext`.

### Movimientos entre discos

`os.rename` es atómico dentro del mismo volumen, pero **falla entre volúmenes distintos**.
De `C:` a `D:` hay que copiar y borrar, lo que no es atómico y puede tardar.

Protocolo: copiar a un temporal en el destino → verificar tamaño y hash → renombrar el
temporal al nombre final → borrar el origen. Si algo falla en medio, el original **sigue
intacto** y se limpia el temporal.

### Archivos bloqueados

En Windows no se puede mover un archivo que otro proceso tiene abierto. Se reintenta con
backoff y, si persiste, se deja en la bandeja con el motivo. Nunca se fuerza.

### Rutas largas

Windows tiene un límite de 260 caracteres salvo que se use el prefijo `\\?\`. Con carpetas
anidadas y nombres de descarga largos se alcanza más fácil de lo que parece. Todas las
rutas se normalizan a formato extendido antes de operar.

### Bucles del propio watcher

Mover un archivo a una carpeta vigilada genera un evento nuevo, que podría procesarse otra
vez. El executor registra las rutas que él mismo acaba de escribir y el watcher las ignora
durante una ventana corta.

### Duplicados entre carpetas vigiladas

Si un directorio vigilado está dentro de otro, un archivo se detecta dos veces. Al añadir
un directorio se comprueba solapamiento y se avisa.

## Lo que Fileflow no hace nunca

- **No borra archivos del usuario.** Como mucho los manda a la papelera del sistema, y con
  confirmación. La papelera es un undo que el usuario ya sabe usar.
- **No borra tampoco sus propios datos.** Las carpetas y los items se dan de baja con
  `status`; así, volver a añadir una carpeta recupera todo lo que aprendió de ella.
- **No sobrescribe.** Ver colisiones.
- **No toca nada fuera de los directorios configurados.**
- **No mueve sin dejar registro**, ni siquiera en modo automático.
- **No sube nada a ninguna parte.** Todo el procesamiento es local.
