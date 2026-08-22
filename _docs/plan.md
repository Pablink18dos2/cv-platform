# CV Platform — Plan

> Especificación del producto. Es la fuente de verdad de **qué** construimos y **para quién**.
> Si un acceptance criterion está mal, se corrige aquí y en el issue, nunca en el código.

## What this is

Plataforma local de visión por computador para montar y explorar prototipos de análisis de
vídeo sin coste de cloud. Un **proyecto** es un fichero de configuración que conecta una
fuente de vídeo (connector) con un modelo de detección y uno o varios analizadores de negocio.
Todos los proyectos comparten el mismo frontal: un dashboard desde el que se navega a la vista
de cada uno.

El objetivo real de esta primera versión no es un producto vendible: es validar que la
**arquitectura por bloques** aguanta añadir un connector nuevo, un modelo nuevo o un análisis
nuevo sin tocar el resto del sistema.

## Users

- **Operador de la plataforma** (yo) — define proyectos en YAML, dibuja las zonas de interés
  sobre un fotograma, arranca y para los workers, y revisa por qué un proyecto se ha caído.
- **Observador** — abre el dashboard, entra en un proyecto y lee el vídeo anotado y las
  métricas. No configura nada, no arranca nada, no puede romper nada.

En el MVP ambos roles son la misma persona y no hay autenticación. La separación está aquí
porque condiciona el diseño de la API: todo lo que el observador necesita es de solo lectura.

## The main flow

1. Abro el dashboard y veo la lista de proyectos, cada uno con su estado (corriendo / parado /
   caído) y su KPI principal.
2. Entro en el proyecto *Parking y tráfico*.
3. Veo el vídeo anotado casi en directo: cajas de los vehículos, su ID de seguimiento, las
   zonas de aparcamiento coloreadas según estén libres u ocupadas, y la línea de conteo.
4. Encima del vídeo, tres KPIs: **plazas libres / total**, **vehículos por minuto** y
   **sentido dominante**.
5. Debajo, dos gráficas temporales — ocupación del aparcamiento y flujo de tráfico — con
   selector de ventana (última hora / últimas 24 h).
6. Si el encuadre de la cámara cambia, abro el editor de zonas, redibujo los polígonos sobre
   un fotograma actual, guardo, y el worker recarga la configuración sin reiniciarse.

## Features

**MVP**

- Connectors de vídeo intercambiables: fichero local, directo de YouTube, RTSP, webcam.
- Detección de objetos con YOLO11 (pesos COCO preentrenados), sustituible por otro detector.
- Seguimiento multiobjeto para dar identidad estable a cada vehículo entre fotogramas.
- Analizador de **tráfico**: cruces de línea con sentido, conteo por minuto y densidad de
  vehículos en la escena.
- Analizador de **aparcamiento**: ocupación por plaza definida como polígono, con histéresis
  temporal para que la ocupación no parpadee.
- Persistencia de métricas y eventos, y agregación por minuto.
- API de solo lectura + streaming del vídeo anotado.
- Dashboard con lista de proyectos y vista de detalle compartida por todos.

**Después del MVP**

- Editor visual de zonas en el navegador.
- Aceleración con OpenVINO sobre la iGPU Intel.
- Más analizadores (tiempo medio de estancia por plaza, colas, mapa de calor).

## Non-goals

Lo que este proyecto **no** hace. Esta lista vale tanto como la de features: es la que impide
que el sistema crezca por los lados.

- **No lee matrículas ni identifica personas.** Ni en MVP ni después. Es una fuente pública y
  la plataforma cuenta objetos, no sigue individuos.
- No hace re-identificación de un vehículo entre cámaras distintas.
- No entrena modelos propios. Se usan pesos preentrenados; el fine-tuning es otro proyecto.
- No usa inferencia en cloud de pago. Todo corre en el portátil.
- No tiene multiusuario, login ni permisos.
- No manda alertas ni notificaciones.
- **No detecta automáticamente dónde están las plazas de aparcamiento.** Se dibujan a mano.
  Inferir la geometría del parking desde el vídeo es un problema abierto y no es este proyecto.
- No garantiza tiempo real estricto. Muestreamos fotogramas; no procesamos los 25 fps.

## Restricciones de hardware

Condicionan el diseño entero, así que están en la spec y no en una nota al pie:

| | |
|---|---|
| CPU | Intel i7-1365U (2 P-cores + 8 E-cores, 15 W) |
| GPU | Intel Iris Xe integrada — **no hay CUDA** |
| RAM | 32 GB |

Consecuencias de diseño, no opiniones:

- Modelo **nano** (YOLO11n) por defecto. Los tamaños s/m/l se pueden configurar, pero la
  configuración por defecto tiene que ir fluida en esta máquina.
- La tasa de inferencia se **desacopla** de la del vídeo. Cada analizador declara la frecuencia
  que necesita: ~5 fps para tráfico (mínimo para que el tracker asocie IDs), ~0,5 fps para
  aparcamiento (una plaza no cambia en dos segundos).
- El worker de inferencia es un proceso **separado** de la API. Es lo que permitirá más
  adelante desplegar API y frontend en un plan gratuito de cloud mientras el trabajo pesado
  se queda en casa.

## Acceptance criteria (MVP)

- [ ] Puedo arrancar un worker apuntando a un fichero de proyecto y procesa el directo de
      YouTube durante 30 minutos seguidos sin intervención.
- [ ] Si la URL del directo caduca a mitad de ejecución, el worker la vuelve a resolver y
      continúa, dejando constancia en el log.
- [ ] El dashboard lista los proyectos existentes y puedo entrar en uno desde ahí.
- [ ] La vista de proyecto muestra el vídeo anotado con una caja y un ID por vehículo.
- [ ] La vista muestra "plazas libres / total" y el número baja cuando un coche aparca en una
      zona definida.
- [ ] La vista muestra vehículos por minuto desglosado por sentido de circulación.
- [ ] Hay una gráfica de la ocupación del aparcamiento de las últimas 2 horas.
- [ ] Los analizadores tienen tests que corren con detecciones sintéticas, sin vídeo ni red.
- [ ] El sistema completo arranca con `docker compose up`.
- [ ] `uv run pytest` pasa en verde y se ejecuta en CI en cada push.

## Tech stack

Decidido en la Fase 1. Cada elección incluye por qué, incluido "porque sé revisarla".

| Bloque | Decisión | Por qué |
|---|---|---|
| Lenguaje | Python 3.13 + `uv` | Es donde vive todo el ecosistema de visión. `uv` ya está instalado y resuelve rápido. |
| Detección | `ultralytics` (YOLO11) | Es el estándar de facto, está mantenido, y trae tracker y export a ONNX/OpenVINO en la misma librería. |
| Tracking | ByteTrack (vía ultralytics) | Va incluido, no añade dependencia, y funciona bien con detecciones ruidosas. |
| Geometría / anotación | `supervision` | Polígonos, líneas de conteo y dibujo de anotaciones ya resueltos y testeados. |
| Vídeo | OpenCV + `yt-dlp` + ffmpeg | `yt-dlp` resuelve el HLS del directo; OpenCV/ffmpeg decodifican. ffmpeg ya está en el sistema. |
| API | FastAPI + Uvicorn | Genera el `openapi.yaml` que es el contrato con el frontend, en vez de escribirlo a mano y que se desincronice. |
| Persistencia | SQLite (local) / Postgres (Docker) vía SQLModel | Una sola capa de modelos para los dos entornos. SQLite en WAL aguanta de sobra este volumen. |
| Frontend | React + Vite + Tailwind | Frontal compartido por todos los proyectos, con la comunicación con backend centralizada en un cliente generado desde el OpenAPI. |
| Tests | pytest + Vitest | — |

**Rechazado:**

- **Streamlit** — arranca en una tarde, pero no soporta un dashboard multiproyecto ni el
  streaming de vídeo anotado sin pelearse con el framework.
- **Next.js** — SSR y un runtime Node en producción que un dashboard interno no necesita.
- **Django** — el ORM y el admin no aportan aquí, y el proyecto no es CRUD.
- **Kafka / Redis Streams** entre worker y API — infraestructura que este volumen no justifica.
  La base de datos es la cola. Si algún día hace falta, el bloque `Sink` es donde se cambia.
- **CUDA / GPU cloud** — restricción explícita de coste.

## Riesgos conocidos

No son excusas por adelantado: son las cosas que hay que medir pronto porque pueden invalidar
el enfoque.

1. **La URL del directo caduca.** Las URLs HLS que devuelve `yt-dlp` viven horas. El connector
   tiene que re-resolver, no morir. Es la causa número uno de que estos prototipos aparezcan
   caídos por la mañana.
2. **Rendimiento en CPU.** Hay que medir los fps reales de YOLO11n a 640 px en esta máquina
   antes de dar por buena la tasa de muestreo. Si no llegan, la salida es OpenVINO o bajar la
   resolución, no bajar el alcance.
3. **Oclusión y perspectiva.** Un coche aparcado detrás de otro puede no verse. La ocupación
   medida es una aproximación; hay que decir cuál es su fiabilidad, no presentarla como exacta.
4. **Noche y lluvia.** Los pesos COCO caen bastante con poca luz. Conviene ver la métrica de
   una noche antes de sacar conclusiones de la serie temporal.
5. **La cámara se mueve.** Si el operador del directo cambia el encuadre, todos los polígonos
   dejan de valer. De ahí que el editor de zonas y la recarga en caliente estén en el plan.

## Open questions

- URL concreta del directo de YouTube del primer prototipo. **Bloquea el issue del connector.**
- ¿Se guardan las detecciones individuales o solo los agregados por minuto? Empezamos por
  agregados y una muestra de detecciones; revisar cuando se vea crecer la base de datos.
- Dónde se despliega la API/frontend cuando toque (criterio 10 del checklist del curso).
