# Rendimiento medido

Medidas reales de YOLO11n en la máquina de desarrollo, sobre el directo del proyecto 01.
Responden al riesgo 2 de [`plan.md`](plan.md), que hasta ahora era una suposición.

Reproducible con `uv run python scripts/spike_yolo_perf.py`.

- **Máquina:** Intel i7-1365U (2 P-cores + 8 E-cores, 15 W), 32 GB, sin CUDA
- **Software:** torch 2.13 CPU, ultralytics 8.4.126, YOLO11n
- **Escena:** 1920x1080, `conf=0.3`, clases de vehículo únicamente
- **Método:** se capturan 40 fotogramas a memoria y después se cronometra la inferencia sobre
  esa lista. Medir mientras se lee del directo daría los fps del stream, no los de la máquina.

## Fotograma completo

| imgsz | mediana | p95 | fps | vehículos/fotograma |
|---|---|---|---|---|
| 640 | 130 ms | 305 ms | **7,7** | 16,5 |
| 960 | 212 ms | 440 ms | 4,7 | 21,9 |
| 1280 | 377 ms | 605 ms | 2,7 | 24,7 |

## Recortando la región útil (1632x778, sin el fondo lejano)

| imgsz | mediana | p95 | fps | vehículos/fotograma |
|---|---|---|---|---|
| 640 | 177 ms | 360 ms | **5,6** | 21,3 |
| 960 | 285 ms | 455 ms | 3,5 | 25,1 |

> ⚠️ **La conclusión 2 de esta sección resultó ser falsa.** La medición controlada de más
> abajo, hecha sobre los mismos fotogramas, la corrige. Se conserva el texto original a
> propósito: es el ejemplo de por qué la salvedad de método del final no era una formalidad.

## Qué se concluye

**1. El primer plano ya está saturado a 640.** Subir el tamaño de inferencia casi no aporta
donde más falta hace: en la franja inferior se pasa de 6,7 a 7,0 vehículos por fotograma entre
640 y 1280, un 4 %. Todo lo que gana el 1280 lo gana en el fondo lejano, que es justo la zona
donde ya habíamos decidido no medir ocupación. **Para el analizador de aparcamiento, 640 sobre
el primer plano es suficiente.**

**2. Recortar rinde mucho más que agrandar.** Recortar a la región útil y correr a 640 detecta
más vehículos en esa región que el fotograma completo a 1280, a menos de la mitad de coste
(177 ms frente a 377 ms). Tiene sentido: recortar sube la resolución efectiva sobre lo que
importa en vez de repartirla por toda la imagen, cielo y fachadas incluidos.

Consecuencia de diseño para el issue 6: **el `Detector` acepta una región de interés opcional**
y el recorte se hace antes de inferir, devolviendo las coordenadas en el sistema del fotograma
original. No es una optimización prematura: es la diferencia entre 5,6 fps y 2,7 fps.

**3. Los presupuestos de fotogramas del plan se sostienen.** Con 5,6 fps útiles hay margen para
los ~5 fps que necesita el tracker del analizador de tráfico. Si al añadir tracking y
analizadores no llegara, la primera salida es OpenVINO (issue 17), no bajar el alcance.

**4. Hay que diseñar para el jitter, no para la media.** El p95 casi triplica la mediana
(305 ms frente a 130 ms). Es lo esperable en un chip de 15 W repartiendo trabajo entre P-cores
y E-cores. Confirma la decisión de la arquitectura de **descartar fotogramas en lugar de
encolarlos**: con una cola, cada pico de latencia se acumularía como retraso permanente
respecto al directo.

**5. Los autobuses no se clasifican como `bus`.** El autobús urbano de la escena sale como
`car` a 640 y como `truck` a 960/1280. La clase `bus` de COCO no aparece nunca. No afecta al
conteo total de vehículos, pero **cualquier métrica desglosada por tipo de vehículo sería poco
fiable en esta escena**, y no conviene ponerla en el dashboard sin avisar.

## Salvedad sobre el método

Las dos tablas se midieron sobre capturas distintas del directo, con minutos de diferencia y
por tanto con tráfico distinto. La comparación de vehículos por fotograma es **indicativa, no
un A/B controlado**: el orden de magnitud de la diferencia (~50 %) es demasiado grande para ser
ruido de muestreo, pero el número exacto no es de fiar.

Se arregla en el issue 6: guardar un clip corto como fixture y medir todas las
configuraciones sobre los mismos fotogramas.


---

# Medición controlada (issue #6)

La tabla de arriba comparaba capturas tomadas en momentos distintos del directo, con tráfico
distinto. Esta compara **exactamente los mismos 60 fotogramas** del fixture
`tests/fixtures/video/calle-1080p-6s.mp4`, así que las diferencias son atribuibles a la
configuración y no al azar de qué pasaba por la calle.

Reproducible con `uv run python scripts/benchmark_detector.py`. Misma máquina, YOLO11n,
`conf=0.3`, clases de vehículo. La región de interés es `BBox(288, 302, 1920, 1080)`: la escena
sin el cielo, sin las fachadas y sin el fondo lejano.

| configuración | mediana | p95 | fps | vehículos/fotograma |
|---|---|---|---|---|
| completo 640 | 136 ms | 386 ms | 7,3 | 16,4 |
| completo 960 | 250 ms | 481 ms | 4,0 | 20,4 |
| completo 1280 | 515 ms | 896 ms | 1,9 | 26,8 |
| **roi 640** | **153 ms** | 915 ms | **6,5** | **20,2** |
| roi 960 | 284 ms | 574 ms | 3,5 | 21,3 |

## Lo que cambia respecto al spike

**El spike afirmaba que recortar a 640 detecta más vehículos que el fotograma completo a 1280.
Es falso.** Medido sobre los mismos fotogramas, 1280 detecta 26,8 por fotograma y el recorte a
640 detecta 20,2: un 33 % más, no menos.

El error tenía una causa concreta y vale la pena entenderla. El recorte excluye el fondo lejano,
que es justo donde el 1280 encuentra la mayor parte de sus detecciones extra. El spike comparó
el total del recorte contra el total del fotograma completo **menos** su franja de fondo, y
además lo hizo sobre muestras distintas. Dos aproximaciones que iban en la misma dirección.

Hay también una razón geométrica que el spike no tuvo en cuenta: inferir a `imgsz` reduce el
lado largo a ese tamaño. El fotograma completo a 1280 reduce 1920→1280, un factor de 1,5. El
recorte a 640 reduce 1632→640, un factor de 2,55. **El recorte a 640 tiene peor resolución
efectiva que el fotograma completo a 1280**, así que era esperable que detectara menos. La
intuición de "recortar concentra la resolución" solo se sostiene comparando contra el mismo
`imgsz`.

## Lo que sí se sostiene, y es lo que decide la configuración

Comparando a igualdad de `imgsz`, recortar **sí** gana, aunque menos de lo que decía el spike:

- **roi 640 frente a completo 640**: 20,2 vehículos frente a 16,4, un **23 % más**, por un 12 %
  más de coste (153 ms frente a 136 ms). Sale a cuenta.
- **roi 640 frente a completo 960**: prácticamente el **mismo número de vehículos** (20,2 frente
  a 20,4) con un **63 % más de fps** (6,5 frente a 4,0). Este es el argumento bueno para el ROI:
  no detecta más que agrandar, detecta **lo mismo mucho más rápido**.
- **completo 1280 queda descartado por otra razón**: detecta más, pero a 1,9 fps. El analizador
  de tráfico necesita unos 5 fps para que el tracker asocie identidades, así que 1280 no es una
  opción por mucho que vea mejor.

**Configuración elegida para el proyecto 01: `imgsz: 640` con el ROI.** 6,5 fps deja margen
sobre los 5 que necesita el tracker, con la misma capacidad de detección que 960 a pantalla
completa.

## Dos avisos que siguen en pie

**El jitter empeora con el ROI.** El p95 de `roi 640` es de 915 ms frente a una mediana de
153 ms: un factor de seis. Es el peor de la tabla. Refuerza, otra vez, que el pipeline tiene que
descartar fotogramas y no encolarlos.

**El desglose por tipo de vehículo no es fiable en esta escena.** La clase `bus` de COCO **no
aparece ni una vez** en ninguna configuración, pese a que en la escena hay autobuses urbanos. Y
los `truck` bailan sin sentido según la configuración: 85, 57, 15, 220, 21 sobre los mismos 60
fotogramas. El conteo **total** de vehículos es estable y utilizable; cualquier desglose por
tipo no lo es, y no debe aparecer en el dashboard sin un aviso.


---

# Tracking: el umbral de activación por defecto pierde el 40 % (issue #7)

Medido sobre 30 fotogramas del fixture de 1080p a `target_fps: 5`, con el detector a
`imgsz: 640` y `conf: 0.35`. 419 detecciones en total.

**La confianza de esta escena es baja**: mínimo 0,35, **mediana 0,62**, máximo 0,85. Solo el
**33 %** de las detecciones llega a 0,7.

Eso choca de frente con el `track_activation_threshold` por defecto de ByteTrack, que es
justamente 0,7: una detección por debajo de ese valor nunca llega a iniciar un track.

| `track_activation_threshold` | identidades distintas | vehículos seguidos por fotograma |
|---|---|---|
| **0,7** (por defecto) | 7 | 5,4 |
| 0,5 | 10 | 8,8 |
| 0,4 | 10 | 8,8 |
| 0,35 | 10 | 8,8 |

Con el valor por defecto se siguen **5,4 vehículos por fotograma de los ~14 que el detector
ve**. Bajando a 0,5 se siguen 8,8, un 63 % más, y **por debajo de 0,5 no se gana nada más**:
hay meseta, no una pendiente en la que cada bajada compre un poco de ruido.

## Qué se hace con esto

El valor por defecto del esquema **se deja en 0,7**, que es el de la librería: afinar umbrales
para esta escena es trabajo del issue #14, con el vídeo delante y viendo los ids dibujados. Lo
que no se puede hacer es dejar el dato sin escribir, porque un `parking_occupancy` construido
sobre 5,4 de 14 vehículos daría una ocupación sistemáticamente baja y nadie sabría por qué.

Dos consecuencias para más adelante:

- **#14 debería empezar por 0,5**, no por el defecto.
- **#11 (aparcamiento) debería plantearse si necesita tracks o le bastan las detecciones.** Una
  plaza ocupada lo está aunque el coche no tenga identidad confirmada, y el tracking es un
  filtro que aquí solo resta. El conteo de tráfico de #10 sí los necesita.

---

# El encadenado completo contra el directo (issue #8)

Hasta aquí todo estaba medido **por piezas**: el detector solo, sobre fotogramas ya capturados
en memoria. Esta es la primera medida del pipeline entero —fuente, detector y tracker a la vez—
contra el directo real, que es lo que dice si el presupuesto del plan se sostiene.

Reproducible con:

```
uv run cvp run projects/01-parking-trafico --duration 220 --health-every 30
```

Misma máquina (i7-1365U, 15 W, sin CUDA). `format: "96"` (1080p30), `imgsz: 640` con el ROI
`[0.15, 0.28, 1.0, 1.0]`, `conf: 0.35`, `target_fps: 5`, `analyzers: []` y `sinks: []`.

```
ejecucion 20260823T213721Z-b6b1ec43
  duracion total:  220.1 s (arranque 9.8 s + bucle 210.3 s)
  fotogramas:      178 (0.85 fps de salida)
  espera fuente:   p50 209.1 ms, p95 3314.3 ms, media 829.5 ms (178 fotogramas)
  detector:        p50 220.7 ms, p95 860.7 ms, media 350.5 ms (178 llamadas)
  tracker:         p50 1.5 ms, p95 2.5 ms, media 1.6 ms (178 llamadas)
  analizadores:    ninguno
  sinks:           0 escrituras, 0 errores
  reintentos de arranque: 0
```

Dos ejecuciones de 220 s dieron **0,85 fps las dos**, con **cero reconexiones** y cero
renovaciones de URL. El arranque (resolver la URL con yt-dlp, cargar los pesos y calentar el
modelo) cuesta entre 10 y 13 s y se paga una vez; por eso va aparte y no se cuenta en los fps.

## El presupuesto del plan **no se sostiene**, y no es culpa del modelo

El plan asume ~5 fps para el tracker de tráfico y ~0,5 fps para el aparcamiento. El encadenado
da **0,85 fps**: seis veces menos de lo que necesita el conteo de tráfico. Con eso, el
aparcamiento (`every_n_frames: 10`) mediría una vez cada 12 s, que probablemente le vale, pero
el tracker de #10 no puede asociar identidades a ese ritmo.

**El detector no es el cuello de botella.** Su mediana dentro del encadenado es de 220 ms —da
para 4,5 fps— y el tracker cuesta 1,5 ms, que es ruido. Lo que se lleva el tiempo es **esperar
a que la fuente entregue el siguiente fotograma**: mediana 209 ms, pero media 830 ms y p95 3,3 s.

## Por qué espera: el directo llega a ráfagas y el consumidor solo muestrea dentro de ellas

Medido con el connector **solo**, sin detector y sin nada más compitiendo por la CPU, durante
60 s (`format: "96"`, `target_fps: 5`):

| | valor |
|---|---|
| fotogramas emitidos al consumidor | 94 = **1,55 fps** |
| fotogramas decodificados por el hilo lector | 2089 = **34,4 fps** |
| espera del consumidor, p50 | 207 ms |
| espera del consumidor, p95 | 3964 ms |
| esperas de más de 1 s | 11 de 94, y suman **43,3 s de los 60,7 s** |
| fotogramas publicados durante cada espera | 1, 31, 22, 23, 31, 30, 1, 15, 31, 28, ... |

El hilo lector va sobrado: decodifica los 30 fps del directo sin despeinarse. Lo que pasa es
que **no los entrega repartidos en el tiempo**. Los publica a ráfagas: ~28 fotogramas en los
200 ms de una espera (unos 135 fps, o sea un segmento HLS entero decodificado de golpe) y
después nada durante ~4 s, hasta que YouTube publica el siguiente segmento.

El consumidor de `cvp/sources/live.py` pide **un fotograma más nuevo que el último que vio** y
se pacea contra el reloj de pared. Dentro de una ráfaga eso funciona: coge uno cada 200 ms.
Entre ráfagas se queda bloqueado, porque no hay ninguno más nuevo. Resultado: solo muestrea
durante la ráfaga, y el 71 % del tiempo está esperando.

Bajar la resolución **empeora** el número, lo que confirma el diagnóstico: con `format: "93"`
(360p) el consumidor recibe 0,42 fps, porque la ráfaga se decodifica aún más rápido y caben
menos ranuras de 200 ms dentro de ella.

## Dos consecuencias que hay que arreglar en el connector, no en el runner

**1. El muestreo se pacea contra el reloj de pared y debería pacearse contra el vídeo.**
`target_fps: 5` sobre una fuente de 30 fps debería significar "uno de cada seis fotogramas
decodificados", que es un muestreo uniforme del vídeo. Paceado contra el reloj, sobre un
transporte a ráfagas, significa "los que caigan mientras dure la ráfaga".

**2. Los `timestamp` de los fotogramas están comprimidos.** `Frame.timestamp` es el instante en
que el hilo lector **leyó** el fotograma, y dentro de una ráfaga se leen 5 s de vídeo en ~1 s.
Los fotogramas que ve el analizador están separados ~200 ms de reloj pero ~900 ms de vídeo, y
después hay un salto de 4 s. Cualquier agregación por minuto, cualquier "vehículos por minuto"
y cualquier histéresis temporal construidos sobre esos timestamps saldrán deformados.

Hay además un detalle menor y barato en el mismo sitio: `_emitir` calcula la siguiente ranura
de paceo **después** de que el consumidor devuelva el control, así que suma los 200 ms del
periodo al tiempo de proceso en vez de solaparlos. Son ~200 ms por fotograma regalados.

Los tres son de `cvp/sources/live.py` (#5), que #8 tiene prohibido tocar. Van anotados en el
issue 8 para que salga la tarea correspondiente.

## Lo que sí se sostiene

- **`imgsz: 640` con ROI sigue siendo la configuración correcta.** Dentro del encadenado su
  mediana es de 220 ms (4,5 fps) frente a los 153 ms del banco de pruebas; el 44 % de más es
  competir por la CPU con el hilo que decodifica el directo.
- **El tracker es gratis**: 1,5 ms de mediana, tres órdenes de magnitud por debajo del detector.
  La decisión de #7 no tiene coste medible.
- **El jitter sigue siendo el de siempre**: p95 del detector 861 ms contra una mediana de
  221 ms, un factor de cuatro. Descartar fotogramas en vez de encolarlos sigue siendo correcto.
- **La fuente aguantó 2 x 220 s sin una sola reconexión** y sin renovar la URL.

# El connector en directo, muestreando en tiempo de vídeo (issue #23)

La medición de #8 dejó dos cosas dichas: el encadenado daba **0,85 fps** de los 5 pedidos y los
`timestamp` salían comprimidos. Las dos tenían la misma causa —el consumidor se paceaba contra
el reloj de pared sobre un transporte que entrega a ráfagas— y las dos se arreglan en el mismo
sitio. Esto es la medición de después, sobre el mismo directo, la misma máquina y la misma
configuración (`format: "96"`, `imgsz: 640` con ROI, `conf: 0.35`, `target_fps: 5`,
`analyzers: []` y `sinks: []`).

Reproducible con:

```
uv run cvp run projects/01-parking-trafico --duration 200 --health-every 100
```

## El antes y el después

| | #8 (antes) | #23 sin las dos optimizaciones | #23 completo |
|---|---|---|---|
| fps de salida | **0,85** | 3,20 | **3,68 · 3,82** |
| espera de la fuente, p50 | 209 ms | 0,0 ms | 0,0 ms |
| espera de la fuente, media | **830 ms** | 7,4 ms | **3,6 · 6,9 ms** |
| espera de la fuente, p95 | 3314 ms | 0,1 ms | 0,1 ms |
| detector, p50 | 221 ms | 196 ms | 192 · 173 ms |
| detector, media | 351 ms | 302 ms | **265 · 252 ms** |
| detector, p95 | 861 ms | 831 ms | **623 · 655 ms** |

Dos ejecuciones de 200 s con el código completo, sin una sola reconexión ni un solo re-anclaje
por deriva. La última columna trae los dos valores.

```
ejecucion 20260824T143606Z-247b8a86
  duracion total:  200.0 s (arranque 11.0 s + bucle 189.0 s)
  fotogramas:      722 (3.82 fps de salida)
  espera fuente:   p50 0.0 ms, p95 0.1 ms, media 6.9 ms (722 fotogramas)
  detector:        p50 173.2 ms, p95 655.2 ms, media 251.8 ms (722 llamadas)
  tracker:         p50 3.2 ms, p95 4.5 ms, media 3.2 ms (722 llamadas)
  analizadores:    ninguno
  sinks:           0 escrituras, 0 errores
  reintentos de arranque: 0
```

**La fuente ha dejado de ser el cuello de botella.** Esperarla costaba 830 ms de cada
fotograma —el 66 % del presupuesto— y ahora cuesta entre 3,6 y 6,9 ms, entre el 1,3 % y el
2,6 % de los 262-271 ms que dura un fotograma. El p95 pasa de
3,3 s a 0,1 ms: ya no hay huecos de cuatro segundos entre ráfagas.

## Los timestamps, que eran la parte grave

150 fotogramas seguidos del directo real, medidos con el connector solo:

```
hueco mediano : 200 ms   (objetivo 200 ms +-10 %)
hueco min/max : 200 / 200 ms
saltos > 1 s  : 0
crecientes    : True
todas en UTC  : True
```

Antes salían separados 200 ms de reloj sobre fotogramas que cubrían ~900 ms de vídeo, con saltos
de 4 s entre ráfagas. Ahora son 200 ms de vídeo clavados, que es lo que `target_fps: 5` promete.
Esto era lo que iba a deformar en silencio los vehículos por minuto de #10, la histéresis de #11
y los minutos de #9: gráficas con forma plausible y mal contenido.

## De dónde salen los fps, uno por uno

El muestreo en tiempo de vídeo arregla los timestamps, pero **por sí solo no sube los fps**. Se
ve aislándolo sobre una tercera fuente, medida en la hora en que YouTube estaba bloqueando esta
máquina (#25): el directo 1080p30 de Red Bull TV
(`https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8`), tres ejecuciones de
180 s seguidas:

| versión | fps | espera de la fuente (media) | detector (media) |
|---|---|---|---|
| antes de #23 | 0,79 | 696 ms | 567 ms |
| muestreo en tiempo de vídeo | 0,81 | **0,1 ms** | 1242 ms |
| \+ `grab()` antes de `retrieve()` | **2,67** | 25 ms | 349 ms |

La segunda fila es el resultado importante y no es intuitivo: la espera de la fuente desaparece
del todo y **los fps no se mueven**, porque el tiempo que el consumidor ya no pasa esperando se
lo come el detector. La máquina estaba saturada antes y sigue estándolo; lo único que cambia es
a quién se le imputa el tiempo.

Lo que sí sube los fps es **dejar de pagar por lo que se descarta**. El hilo lector llama a
`grab()` para todos los fotogramas —descodificar hay que descodificarlos, los de H.264 dependen
unos de otros— pero solo llama a `retrieve()` para el que sale. Cinco de cada seis se ahorran la
conversión de color y la copia de 6 MB de un 1080p, y esa CPU es exactamente la que necesita el
detector.

## Cuántos hilos se le dan al decodificador

Por defecto ffmpeg coge uno por núcleo. En esta máquina —2 P-cores y 8 E-cores de 15 W— eso
significa que el decodificador y el detector se pelean por los mismos dos núcleos rápidos todo
el rato. Con `CAP_PROP_N_THREADS`, sobre el directo real y 200 s por ejecución:

| hilos de ffmpeg | fps de salida | detector p95 | detector media |
|---|---|---|---|
| sin límite (por defecto) | 3,20 | 831 ms | 302 ms |
| **2** | **3,68 · 3,82** | 623 · 655 ms | 265 · 252 ms |
| 1 | 2,26 | 1418 ms | 439 ms |

Dos es el punto bueno, y los dos extremos fallan por motivos distintos: sin límite, el
decodificador le roba los P-cores al detector y le alarga la cola; con uno solo, el
decodificador se convierte él en el cuello. Que sobra margen de decodificación está medido
aparte: sobre un directo 1080p30, ffmpeg descodifica a 42 fps con dos hilos frente a 49 fps sin
límite, y solo hacen falta 30.

## El presupuesto sigue sin sostenerse, pero ya no por la fuente

3,7-3,8 fps son **4,4 veces** los 0,85 de #8, y aun así se quedan por debajo de los 5 fps que
pide el tracker de tráfico del plan, y por debajo de los 4 fps que pedía el criterio de #23.
El desglose dice exactamente dónde está el techo: la media del detector es de 252-265 ms, o sea
3,8-4,0 fps, y la espera de la fuente es el 2,6 % del presupuesto. **El cuello de botella se ha
movido de la fuente al detector.**

Es un resultado, no un fracaso a medias: hasta ahora optimizar el modelo no habría comprado
nada, porque el 66 % del tiempo se iba en esperar a la fuente. Ahora sí lo compra, y por eso el
siguiente paso es **#17 (OpenVINO)**. Lo que hace falta es poco: bajar la media del detector de
265 ms a 200 ms pone el encadenado en 5 fps.

Queda además un aviso que sigue en pie y que sale en el log de todas las ejecuciones: **el
análisis no sostiene los 5 fps pedidos**, así que el buffer del connector va lleno y descarta
los fotogramas más antiguos. El retraso respecto al directo se queda en los 4 s del buffer y no
crece, que es lo que se quería, pero mientras el detector no baje de 200 ms el `target_fps: 5`
del proyecto está pidiendo más de lo que la máquina da.

# El muestreo se ajusta a lo que el análisis sostiene (issue #23, segunda ronda)

La primera ronda de #23 arregló el muestreo en tiempo de vídeo, y la verificación encontró que
seguía habiendo saltos en la serie **en el régimen en el que corre el proyecto**: un consumidor
más lento que `target_fps`. Los timestamps salían con mediana perfecta de 200 ms y un salto de
1,2 s por ciclo de segmento.

La causa: pedir 5 fps a una máquina que sostiene 3,7 seleccionaba 5, llenaba el buffer y el
descarte caía en una **tirada contigua** de la cabeza de la cola, así que al análisis le
desaparecía un bloque entero de golpe. La mediana no lo veía, y ninguna de las dos mediciones de
timestamps lo vio porque las dos se tomaron **con el connector solo**, que es la única
configuración donde el buffer no se llena.

Dos cambios:

- **El muestreo se realimenta.** Cada 4 s se compara cuántos fotogramas se ofrecieron con
  cuántos consumió el análisis. Si no da abasto, la tasa baja a la que ha demostrado sostener;
  si va sobrado, sube a tientas hasta el `target_fps` pedido. Así los huecos salen regulares en
  vez de agrupados, porque no hay nada que descartar.
- **El buffer pasa de 4 s a 8 s de vídeo.** Con 4 s, una ráfaga de un segmento entero (~4,8 s de
  vídeo) llenaba el buffer ella sola y el fotograma siguiente ya desalojaba, aunque el consumidor
  fuera al día. Tiene que caber un segmento y sobrar.

## Lo que cuesta la realimentación: nada medible

La duda razonable era si muestrear por debajo del `target_fps` costaría fps de salida. Medido
back-to-back contra el directo real, 200 s cada uno, con el controlador puesto y desactivado:

| | fps de salida | espera de la fuente (media) | detector (media) |
|---|---|---|---|
| con realimentación | 2,74 | 0,0 ms | 363,5 ms |
| sin realimentación | 2,80 | 0,0 ms | 355,1 ms |

Es la misma cifra dentro del ruido de la máquina. El throughput lo fija la media del detector,
no el muestreo: mientras haya un fotograma esperando —y lo hay, la espera de la fuente es de
0,0 ms en los dos casos— el análisis va a su tope.

**Y cuidado con comparar entre sesiones**: estas dos ejecuciones dan 2,7-2,8 fps donde las de la
primera ronda daban 3,68-3,82, con el mismo código de fuente y detector. La diferencia está
entera en el detector (media de 355-363 ms ahora contra 252-265 ms entonces), o sea en el estado
de la máquina, no en el connector. Por eso la comparación que vale es la de arriba, tomada
seguida; los números entre secciones distintas de este documento no son comparables.
