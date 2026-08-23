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
