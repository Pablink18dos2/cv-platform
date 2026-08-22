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
