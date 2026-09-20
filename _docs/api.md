# API

<!-- Se carga en cualquier tarea que toque la API o el cliente del dashboard (#13). Es el
     documento que hay que leer antes de pintar una grafica: la parte de `metric_minute`
     no es opcional. -->

La API es **el segundo proceso** de [architecture.md](architecture.md): FastAPI sobre la base
de datos y sobre el fichero del ultimo fotograma, de solo lectura, sin cargar ningun modelo.
No sabe nada del worker: ni lo arranca, ni lo para, ni comparte memoria con el. Arranca en
milisegundos y se podria desplegar en un plan gratuito de cloud dejando el worker en casa.

```
uv run cvp api                       # escucha en CVP_API_HOST:CVP_API_PORT (127.0.0.1:8000)
uv run cvp api --port 8080           # la linea de comandos manda sobre el entorno
uv run cvp api --export-openapi      # escribe openapi.yaml y sale, sin servir
```

Lee `DATABASE_URL` —**la misma variable que el worker**, para que los dos miren la misma
base sin ponerse de acuerdo—, `CVP_API_HOST`, `CVP_API_PORT` y `CVP_CORS_ORIGINS`, con los
valores por defecto de `.env.example`. Con la base ausente o vacia arranca y devuelve listas
vacias; no es un error que el worker no haya corrido todavia. Ctrl+C la apaga y sale con 0.

## El contrato: `openapi.yaml`

`openapi.yaml`, en la raiz del repo, es el contrato entre frontend y backend, y **se genera
desde la app**, nunca a mano:

```
uv run cvp api --export-openapi
```

`tests/unit/test_api_contrato.py` falla si el fichero commiteado no coincide con lo que
genera `app.openapi()`. Es lo que lo convierte en contrato y no en documentacion: cambiar
una ruta sin regenerarlo rompe CI en el mismo commit.

- Si cambian las rutas o los modelos de respuesta, se regenera y se commitea **en el mismo
  commit** que el codigo.
- El cliente del frontend se genera desde este fichero. Un endpoint que no este aqui no
  existe; se anade al codigo, se regenera el contrato, y entonces se usa.

> Los agentes de codigo se inventan endpoints. El contrato es lo que impide que eso llegue
> al dashboard.

## Convenciones

- **Base path**: `/api/v1`. Solo `GET`: el MVP es de solo lectura, y arrancar o parar workers
  desde la API no es un objetivo.
- **Sin autenticacion**. No-goal del plan.
- **Timestamps**: ISO-8601 en UTC con `Z` en las respuestas (`2026-08-24T14:20:00Z`), que es
  la forma canonica de `cvp.core.serialization`. En los parametros se acepta cualquier
  ISO-8601 **con zona**; sin zona es un 422. Un `+02:00` en una URL hay que codificarlo
  como `%2B02:00`; con `Z` no hay nada que codificar.
- **Errores, una sola forma**, tambien los que produce la propia infraestructura (una ruta
  que no existe, un metodo que no toca):

  ```json
  { "error": { "code": "project_not_found", "message": "no existe ningun proyecto con id 'x'" } }
  ```

  `code` es estable y es lo que el cliente compara; `message` es para una persona y puede
  cambiar. Los de validacion llevan ademas `details`, un elemento por campo:

  ```json
  { "error": { "code": "validation_error", "message": "parametros invalidos: limit",
               "details": [ { "field": "limit", "message": "Input should be less than or equal to 500" } ] } }
  ```

| Codigo HTTP | `code` | Cuando |
|---|---|---|
| 400 | `invalid_range` | `from` posterior a `to`, o una ventana de metricas de mas de 7 dias |
| 404 | `project_not_found` | el `{id}` no esta en la tabla `project`, o ni tiene forma de id |
| 404 | `snapshot_not_found` | el proyecto no tiene todavia un fotograma en disco |
| 404 | `not_found` | la ruta no existe |
| 422 | `validation_error` | un parametro no pasa la validacion; `details[].field` dice cual |

## Los recursos

| Ruta | Que devuelve |
|---|---|
| `GET /projects` | La lista: `id`, `name`, `status`, `last_seen_at` |
| `GET /projects/{id}` | Lo mismo mas `created_at`, `last_run` (`id`, `started_at`, `ended_at`) y `analyzers` |
| `GET /projects/{id}/metrics` | Series por minuto de `metric_minute`, en una rejilla regular |
| `GET /projects/{id}/metrics/latest` | El ultimo minuto con muestras de cada serie: los KPIs |
| `GET /projects/{id}/events` | Eventos en la forma canonica mas `run_id`, paginados hacia atras |
| `GET /projects/{id}/snapshot.jpg` | El ultimo fotograma, `image/jpeg`, `Cache-Control: no-store` |
| `GET /projects/{id}/stream.mjpg` | El mismo, como `multipart/x-mixed-replace`; no termina |

Los parametros exactos, con sus limites, estan en `openapi.yaml`. Lo que sigue es lo que el
contrato no puede decir: la semantica.

### `status` de un proyecto

Se deriva de la **ultima ejecucion** (`run`, la mayor por `id`: el `run_id` ordena por tiempo)
y del reloj de la API:

- `running`: la ejecucion ha escrito en los ultimos **90 s**.
- `stopped`: escribio alguna vez, pero hace mas de 90 s.
- `stale`: arranco y no ha escrito ninguna muestra (`ended_at` nulo), o no hay ejecuciones.

"Ha escrito" es `run.ended_at`, que desde #9 es **el instante de la ultima muestra**, no el
del apagado: un worker que muere no cierra su ejecucion, y lo que se quiere saber es hasta
cuando hubo datos. `last_seen_at` es ese mismo instante. Dos consecuencias que no se
disimulan: la API no distingue un Ctrl+C de un `kill -9` ni de una fuente caida diez minutos
(el worker no escribe su salud en ningun sitio; es otro issue), y con una fuente de fichero
`ended_at` es tiempo de video, asi que un proyecto que reproduce un video de 2024 sale
`stopped` aunque el worker este corriendo.

### `metric_minute`: lo que hay que saber antes de pintar una grafica

La tabla guarda, por proyecto, minuto UTC y serie `(analyzer, name, labels)`, el agregado de
las muestras de ese minuto. La API **devuelve todas las columnas** de cada punto y **no elige
cual usar**: elegir `sum` o `mean` por el nombre de la metrica acoplaria la API a los
analizadores. Esta es la decision de #12, y lo que sigue es lo que el frontend necesita
para elegir bien. Los cuatro bordes estan medidos (#10, #29), no son teoricos, y los cuatro
producen graficas enganosas si no se tratan.

Un punto:

```json
{ "minute": "2026-08-24T14:20:00Z", "samples": 30, "seconds": 58.5,
  "sum_value": 30.0, "mean_value": 0.51, "min_value": 1.0, "max_value": 1.0, "integral": 29.9 }
```

| Columna | Que es | Para que sirve |
|---|---|---|
| `samples` | muestras del minuto | saber cuanto hay detras del punto; **siempre > 0** (ver borde 3) |
| `seconds` | segundos del minuto cubiertos por muestras | cuanto tiempo se estuvo mirando; **puede pasar de 60** (ver borde 4) |
| `sum_value` | suma de los valores | **el conteo** de una metrica de conteo |
| `mean_value` | `integral / seconds`, media **en el tiempo** | **el valor** de un gauge |
| `min_value`, `max_value` | extremos de las muestras | el rango |
| `integral` | suma de valor x segundos | de donde sale `mean_value`; acumulable |

**Borde 1 — un minuto sin muestras no tiene fila, y un sentido sin cruces tampoco.** Un
analizador solo emite `crossings` cuando hay un cruce, asi que un minuto con 5 vehiculos al
norte y ninguno al sur tiene fila para el norte y **ninguna** para el sur. Lo que hace la
API: la serie es **una rejilla regular**, un punto por minuto de `[from, to]`, y **los minutos
sin fila salen como `null`**, para que el hueco sea explicito. Lo que no hace: decidir si ese
`null` es un cero. Un hueco y un cero cuentan historias distintas y solo el frontend sabe que
metrica esta pintando:

- Para una metrica **de conteo** (`crossings`), `null` **es 0**: no hubo cruces.
- Para un **gauge** (`vehicles_in_scene`, `zone_occupied`, `spaces_free`), `null` es **"sin
  dato"**: no se estaba mirando. Se pinta como hueco, no como cero.

Y la otra mitad del mismo borde: una serie sin **ninguna** fila en la ventana **no aparece**
en la respuesta, porque la API no tiene de donde sacar sus etiquetas. El dashboard que
espera "norte" y "sur" y solo recibe "norte" tiene que pintar el "sur" a cero el mismo; las
direcciones de cada linea estan en el `project.yaml`.

**Borde 2 — `sum_value` para conteos, `mean_value` para gauges.** `crossings` es una metrica
de valor 1 por cruce: la suma del minuto es el conteo, y la media ponderada por tiempo de una
metrica de valor 1 no significa nada. `vehicles_in_scene` y `zone_occupied` son gauges: la
media temporal es la densidad o la fraccion del minuto que la plaza estuvo ocupada, y la suma
no significa nada. La media es **en el tiempo** (`integral / seconds`), no por muestra
(`sum_value / samples`): desde #23 la tasa de muestreo se mueve dentro del minuto y con
sesgo, y una media por muestra aplana justo las horas punta (QA midio hasta un 78 % de error).
No se recalcule la media dividiendo `sum_value` entre `samples`.

| Metrica | Tipo | Columna | Lo que representa |
|---|---|---|---|
| `crossings` | conteo | `sum_value` | vehiculos que cruzaron en el minuto, por linea y sentido |
| `vehicles_in_scene` | gauge | `mean_value` | densidad media del minuto |
| `zone_occupied` | gauge (0/1) | `mean_value` | fraccion del minuto con la plaza ocupada |
| `spaces_free`, `spaces_occupied`, `spaces_total` | gauge | `mean_value` | plazas medias del minuto |

Un analizador nuevo dira en su docstring de que tipo es cada metrica; la API no lo sabe.

**Borde 3 — `samples == 0` es "sin datos", no un cero.** Tras un lote descartado (un fallo de
disco que el runner sobrevive, #8) el agregador ya habia avanzado, y el lote siguiente escribe
el **tramo** de la muestra perdida en un minuto que no tiene su muestra: una fila con
`samples=0, seconds=15, mean=40`, con una media inventada (#29). La API **no la sirve**: en la
serie sale como `null`, y en `latest` se salta al ultimo minuto que si tiene muestras. Todo
punto que llega tiene `samples > 0`.

**Borde 4 — `seconds` puede pasar de 60.** Una ejecucion no escribe mas de 60 s por minuto,
pero dos ejecuciones que midan el mismo minuto —un fichero reprocesado dos veces, o el caso de
#28— **suman**, y `seconds` llega a 120 (#29). La API lo sirve tal cual: recortarlo perderia
informacion y normalizarlo mentiria sobre la media. Quien pinte "cobertura = seconds / 60"
vera un 200 %; la cobertura es `min(seconds, 60) / 60`, y un `seconds > 60` significa
"este minuto se midio mas de una vez".

### La ventana de `metrics`

- `from` y `to` son inclusivos y se truncan al minuto UTC. Sin ninguno, **la ultima hora**
  hasta ahora; con solo `from`, de `from` hasta ahora; con solo `to`, la hora anterior a `to`.
- **Como mucho 7 dias** por peticion (10.081 puntos por serie); mas, 400 `invalid_range`. Sin
  esto una peticion sin `from` sobre una base de meses tumba la API.
- `labels` filtra por igualdad de las claves dadas: `labels=direction:norte` encaja con
  `{"line": "principal", "direction": "norte"}`. Varias, separadas por comas.
- `step` solo admite `minute`. Esta en el contrato para que anadir `hour` no cambie la forma.

### Eventos y paginacion

Cada evento es **exactamente** `event_to_dict(evento)` de `cvp.core.serialization` (#19) mas
`run_id`: `analyzer`, `name`, `timestamp`, `attributes`. Es la misma forma que escribe el sink
`jsonl` y que `event_from_dict` vuelve a leer; no hay una segunda que mantener.

Van por timestamp descendente, `limit` por pagina (100 por defecto, 500 como maximo) y
`before` como cursor **exclusivo**: la pagina siguiente se pide con
`before=<next_before de la anterior>`, y `next_before` es `null` cuando no hay mas. Dos
vehiculos que cruzan en el mismo fotograma producen dos eventos con el **mismo timestamp**,
y la pagina **nunca corta entre ellos**: si el ultimo de la pagina comparte timestamp con el
siguiente, la pagina se alarga hasta incluirlos todos. Puede pasar de `limit` en unos pocos
eventos, y a cambio recorrer las paginas no repite ni salta ninguno.

### El fotograma

`snapshot.jpg` sirve `data/preview/<id>.jpg`, el fichero que el sink `preview` escribe de
forma atomica, con `Cache-Control: no-store`: un `<img>` que se recarga cada pocos segundos no
puede recibir el de hace un minuto de la cache. `stream.mjpg` es el mismo fichero como MJPEG:
lo mira cada 250 ms y **solo emite si ha cambiado** (`mtime` y tamano), asi que con el worker a
5 fps salen 5 fotogramas por segundo y no 4 copias del mismo. No termina por su cuenta; termina
cuando el cliente cierra, que es como funciona `<img src=".../stream.mjpg">`.

Los dos **leen el fichero entero y lo cierran antes de responder**. Es la mitigacion de #27: en
Windows, el `os.replace` del sink falla si alguien tiene el fichero abierto, y un lector que lo
mantuviera abierto mientras transmite haria fallar al worker una de cada cuatro escrituras.
Un cliente nuevo que sirva este fichero por otro camino tiene que hacer lo mismo.

## Anadir un endpoint

1. La consulta, en `cvp/api/queries.py` (no en `cvp/storage/queries.py`: la API no toca
   `cvp.storage` ni `cvp.core`, solo los usa). SQL portable, como todo lo demas.
2. El modelo de respuesta en `cvp/api/schemas.py`, con `description` en los campos que lo
   necesiten: es lo que el cliente generado vera en su editor.
3. La ruta en su router, con `operation_id` (es el nombre del metodo en el cliente generado),
   `summary`, y los errores que puede devolver en `responses`.
4. `uv run cvp api --export-openapi`, y el contrato va en el mismo commit.
5. Tests en `tests/unit/test_api_*.py`, con la base poblada por los sinks reales
   (`tests/unit/api_soporte.py`), no con `INSERT`.

Y lo que no puede pasar: **importar nada pesado**. Ni `cvp.sinks` (arrastra cv2), ni
`cvp.pipeline.runner`, ni ningun bloque del worker. `tests/unit/test_api_aislamiento.py` lo
comprueba en un subproceso, y es lo que mantiene a la API desplegable donde el worker no cabe.
