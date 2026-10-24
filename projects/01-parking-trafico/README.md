# Proyecto 01: parking y tráfico (Collingwood)

Configuración real del directo de `fuente.md`. Geometría dibujada sobre
`reference-frame-2026-10-18.jpg` (el mismo encuadre que `reference-frame.jpg` de agosto).

## Qué mide

- **`trafico` (`traffic_flow`)**: cruces de la línea `calzada` (tercio inferior, y 0,77-0,84,
  cubre los dos carriles y ninguna plaza) con sentido `hacia_camara` / `hacia_fondo`, y
  vehículos en escena. Cuenta con el punto inferior central de cada track.
- **`plazas` (`parking_occupancy`)**: ocupada/libre de **11 plazas** de `zones.json`, con
  histéresis de 6 s para ocupar y 10 s para liberar, una muestra por segundo.
- **`framing_guard`**: avisa (no suprime nada) si la cámara se mueve respecto a la referencia.

## Cuántas plazas se vigilan

**11 de unas 30-35 visibles** en el encuadre (estimación contando coches en capturas con la
calle llena). 9 a la derecha (`der-1` lejana a `der-9` cercana) y 2 a la izquierda.

- Derecha: bahías en batería entre dos líneas pintadas visibles. `der-1` y `der-2` son plano
  medio (coches de ~90 px) y las menos fiables. `der-9` es `bahia-3` del fixture, cortada por el
  borde derecho (IoU 0,38 con coche en la referencia, justo sobre el umbral de 0,35).
- Izquierda: **solo `izq-1` e `izq-2`**. No tienen líneas pintadas visibles, así que el polígono
  sigue la posición de los coches; el detector las acierta en las tres capturas probadas (referencia y dos del directo).
  Las otras dos de primer plano (pickup rojo y pickup negro) **se descartaron**: en la
  referencia (lado a la sombra) el detector no las ve con el coche presente, y una plaza que
  falla persistentemente cuenta como libre. Los árboles tienen hoy menos hoja; **en primavera
  volverán a tapar ese lado**: revisar `izq-*` entonces.

## Qué NO concluir

- El KPI es **"N libres de 11 monitorizadas"**, nunca "plazas libres de la calle".
- El desglose por tipo de vehículo no es fiable (el detector no lo ha medido por clase; no
  usarlo).
- **La noche no está verificada**: ni el detector, ni las zonas, ni el guardián de encuadre
  (podría dar falsas alarmas con la cámara quieta).
- Un coche que circula pegado a las bahías puede ocupar una plaza unos segundos; la histéresis
  lo absorbe salvo que se pare. Un autobús que tape varias plazas puede liberarlas tras 10 s.
- Un track oculto más de ~1 s (el tracker lo pierde a 5 fps) recibe otra identidad: puede perderse
  un cruce, no duplicarse.

## Ejecución de 30 minutos (2026-10-24, 17:15–17:45 UTC, tarde soleada)

- Estabilidad: 7150 fotogramas, 3,99 fps de salida (detector p50 162 ms, p95 410 ms), 0 errores
  de sinks, 0 reconexiones, 0 reintentos de arranque. El muestreo adaptativo se mantuvo entre
  ~3 y 5 fps sin perder bloques.
- Ocupación: 2–4 libres de 11 (media 2,7), 15 cambios de estado en 30 min, casi todos en `der-3`
  (el borde de la fila, donde un coche que pasa lo ocupa y lo libera). El último preview se
  comparó a ojo con la métrica: 3 libres (`der-1`, `der-2`, `der-3`), coincide con lo que se ve.
  Dudosa: `der-1`/`der-2` tienen una sombra oscura dentro que podría ser un coche parcialmente
  visible.
- Tráfico: 482 cruces de la línea `calzada` (250 `hacia_camara`, 232 `hacia_fondo`, ~16/min),
  repartidos de forma simétrica. **No se ha contado a mano**: es plausible, no verificado.
- Guardián de encuadre: `framing_score` medio 0,81, sin `framing_lost`. Hubo muestras sueltas
  a 0,0 (17:25, 17:32, 17:33) sin explicación comprobada; el guardián no saltó porque exige 30 s
  seguidos por debajo del umbral.

## Qué falta por verificar

- Precisión de ocupación frente a un recuento manual en varias horas del día (solo se han mirado unas
  pocas capturas).
- Precisión de los conteos y del sentido frente a un recuento manual.
- Las muestras de `framing_score` = 0,0: ver qué pasa en esos fotogramas (¿decodificación?).
- Noche, lluvia y nieve.
- Una serie de varias horas: no es fiable hasta #26 (deriva de tiempo del decodificador).
