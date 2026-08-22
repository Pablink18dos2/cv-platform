# Fuente del proyecto 01 — Collingwood Live Stream

Reconocimiento hecho el 2026-08-22 sobre el directo real, antes de escribir el connector.
Todo lo de aquí está medido, no supuesto. `reference-frame.jpg` es el fotograma sobre el que
se dibujarán las zonas.

- **URL:** `https://www.youtube.com/watch?v=EPKWu223XEg`
- **Título:** *Town of Collingwood Live Stream*
- **Escena:** calle principal de Collingwood (Ontario), cámara elevada a unos dos o tres pisos,
  mirando calle abajo hacia el punto de fuga.

## Formatos disponibles

| ID | Resolución | FPS | Bitrate |
|---|---|---|---|
| 96 | 1920x1080 | 30 | 4561k |
| 95 | 1280x720 | 30 | 2448k |
| 94 | 854x480 | 30 | 1283k |
| 93 | 640x360 | 30 | 962k |

Todos son HLS muxados (vídeo + audio en una sola URL), así que el connector no tiene que
combinar flujos. **Vamos con el 96 (1080p)**: los coches del fondo ya son pequeños a 1080p y
bajar de ahí los haría indetectables.

## Dos hallazgos que condicionan el connector

**1. El cliente por defecto de yt-dlp falla.** Sin argumentos, devuelve
`ERROR: This video is not available`. Con `--extractor-args "youtube:player_client=web_safari,tv,android"`
resuelve sin problema. El connector tiene que fijar estos clientes y, si YouTube los rompe en
el futuro, el síntoma será exactamente ese error engañoso, no un fallo de red.

**2. La URL HLS caduca en 6 horas exactas.** El parámetro `expire` del manifiesto resuelto a
las 18:02 UTC marcaba las 00:01 UTC. No es una estimación: es el número que trae la URL.
Diseño que se sigue de ahí: re-resolver proactivamente cada ~5 h **además** de reactivamente
cuando la lectura falle. Solo reactivo significa un hueco en los datos cada seis horas.

## Qué se puede medir de verdad en esta escena

La escena tiene un gradiente de perspectiva muy fuerte, y eso decide el alcance honesto del
análisis:

- **Primer plano (tercio inferior)** — las plazas en batería del lado derecho se ven con sus
  líneas pintadas, y los coches ocupan cientos de píxeles. Aquí la ocupación es fiable.
- **Plano medio** — las plazas siguen distinguiéndose pero los coches se solapan entre sí.
  Fiabilidad regular; conviene medirla antes de darla por buena.
- **Fondo (tercio superior)** — los coches son manchas de pocos píxeles y se tapan unos a otros.
  **No se define ninguna zona de aparcamiento aquí.** Cualquier número que saliéramos de esta
  región sería inventado.

Consecuencia para `zones.json`: se dibujan solo las plazas del primer plano y plano medio, y el
KPI se presenta como *"plazas libres de las N monitorizadas"*, nunca como *"plazas libres de la
calle"*.

### Limitaciones concretas de la escena

- **Los árboles de la acera izquierda tapan parcialmente las plazas de ese lado.** El lado
  derecho está mucho más despejado. Empezar por el derecho.
- Hay autobuses y furgonetas grandes que, al pasar, ocultan varias plazas a la vez. Es
  justamente el caso que la histéresis temporal tiene que absorber sin liberar plazas falsas.
- La calle es de doble sentido con línea amarilla central, así que el sentido de circulación se
  puede deducir de la trayectoria del track sin necesidad de calibrar nada.
- Hay peatones en escena. Se filtran por clase: solo `car`, `motorcycle`, `bus`, `truck`.

## Línea de conteo

El sitio bueno es el tercio inferior, cruzando la calzada de lado a lado, donde los vehículos
son grandes y ningún árbol los tapa. Las coordenadas exactas se fijan en el issue 14, sobre
este mismo fotograma.

## Pendiente de comprobar

- **Vista nocturna.** Hay que capturar un fotograma de noche antes de fiarse de la serie
  temporal de 24 h.
- **Estabilidad del encuadre.** Si el ayuntamiento mueve la cámara, todas las zonas dejan de
  valer. Merece un test que compare periódicamente contra este fotograma de referencia.
- **Tamaño de inferencia.** A `imgsz=640` un fotograma 1080p se reduce a un tercio y los coches
  del plano medio pierden mucho. Hay que medir 640 contra 960 en la tarea 6 y decidir con el
  dato de fps delante.
