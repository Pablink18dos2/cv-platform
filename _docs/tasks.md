# Backlog

> Registro histórico. En cuanto estas tareas pasen a GitHub issues, **los issues son el
> backlog canónico** y este fichero no se mantiene. Dos backlogs = ningún backlog.

Orden pensado para que cada tarea deje algo verificable y para que las dependencias fuertes
(contratos, pipeline) estén antes que lo que se apoya en ellas.

---

## 1. Esqueleto del proyecto con un test que pasa

Goal: `uv sync` instala, `uv run pytest` pasa y CI se pone en verde.
Description: Estructura `backend/src/cvp/`, `pyproject.toml` con uv, ruff y pytest configurados, un test trivial y `ci.yml` adaptado a este stack. Sin dependencias de visión todavía: esta tarea es el andamio, no el producto.

## 2. Tipos del core y contratos

Goal: `cvp.core` define `Frame`, `Detection`, `Track`, `Event`, `Metric` y los `Protocol` de cada bloque.
Description: Dataclasses inmutables y los cinco Protocols descritos en `_docs/architecture.md`. Incluye el mecanismo de registro por nombre (`@register_source` y equivalentes) con su test. `core` no importa nada del resto del paquete y no depende de torch ni de OpenCV.

## 3. Configuración de proyecto validada

Goal: un `project.yaml` inválido falla al arrancar diciendo qué campo está mal.
Description: Modelos Pydantic para source, detector, tracker, analyzers y sinks. Carga desde fichero, resolución de rutas relativas al directorio del proyecto y expansión de variables de entorno. Tests con YAMLs buenos y malos.

## 4. Connector de fichero de vídeo

Goal: `FileVideoSource` recorre un mp4 local y emite `Frame` con timestamp e índice.
Description: Primera implementación de `VideoSource`, con muestreo a `target_fps` descartando fotogramas. Incluye un fixture de vídeo corto (pocos MB) en el repo. Es el connector que usarán todos los tests de los demás bloques.

## 5. Connector de directo de YouTube

Goal: `YouTubeLiveSource` procesa un directo durante 30 minutos sin intervención.
Description: Resolución de la URL HLS con `yt-dlp`, decodificación, y re-resolución con backoff exponencial cuando la URL caduca o el stream se corta. La lógica de reintento se testea con un resolvedor falso; el test contra YouTube de verdad va marcado `network` y fuera de la suite por defecto.

## 6. Detector YOLO

Goal: `YoloDetector` devuelve `Detection` normalizadas sobre una imagen de prueba.
Description: Envuelve ultralytics YOLO11n. Filtrado por clase y confianza desde la configuración, descarga y cacheo de pesos en `models/`, y `warmup()`. Nada del formato de ultralytics sale de este módulo. Incluye una medición de fps en esta máquina, que es un dato que necesitamos para el riesgo 2 del plan.

## 7. Tracker ByteTrack

Goal: un vehículo mantiene el mismo `track_id` mientras está en escena.
Description: Adaptador sobre el tracker de ultralytics que devuelve `Track`. Test con una secuencia sintética de detecciones que se desplazan, comprobando estabilidad del ID y que un ID no se reutiliza tras desaparecer.

## 8. Pipeline runner y CLI

Goal: `uv run cvp run projects/01-parking-trafico` arranca el bucle completo.
Description: Ata source, detector, tracker, analizadores y sinks desde el YAML, respetando el `every_n_frames` de cada analizador. Apagado limpio con Ctrl+C, logging estructurado y métricas propias de salud (fps real, fotogramas descartados). Test de componente con `FakeVideoSource` y `FakeDetector`, determinista y sin red.

## 9. Persistencia y sinks

Goal: los eventos y métricas de una ejecución quedan en base de datos y se pueden consultar.
Description: Modelos SQLModel para `project`, `run`, `event`, `metric` y `metric_minute`. Sink SQLite con escritura por lotes y agregación por minuto, sink `preview` en memoria para el último fotograma anotado, y sink `jsonl` para depurar. SQLite en modo WAL.

## 10. Analizador de tráfico

Goal: cuenta vehículos que cruzan una línea, por sentido, agregado por minuto.
Description: Cruce de línea a partir de la trayectoria del `track_id`, con sentido según el lado del que viene. Métrica de densidad (vehículos en escena) y anotación de la línea y los contadores sobre el fotograma. Tests con trayectorias sintéticas, incluidos los casos raros: track que aparece encima de la línea, que la cruza y vuelve, y que desaparece a mitad.

## 11. Analizador de aparcamiento

Goal: informa de plazas libres y ocupadas sobre zonas definidas como polígonos.
Description: Carga `zones.json`, asigna vehículos a plazas por solape, y aplica **histéresis temporal** para que una detección perdida no libere una plaza. Emite eventos de ocupación y liberación y la métrica de plazas libres. Tests con reloj falso que cubren el parpadeo, el coche que pasa por delante sin aparcar y la plaza en la que caben dos detecciones.

## 12. API de lectura y contrato OpenAPI

Goal: el frontend puede listar proyectos y leer métricas sin saber nada del worker.
Description: FastAPI con `/projects`, `/projects/{id}`, `/projects/{id}/metrics`, `/projects/{id}/events`, `/projects/{id}/snapshot.jpg` y `/projects/{id}/stream.mjpg`. `openapi.yaml` exportado desde la app y commiteado, con un test que falla si el fichero se desincroniza del código. Solo lectura: la API no arranca ni para workers en el MVP.

## 13. Dashboard y vista de proyecto

Goal: puedo abrir el navegador, ver la lista de proyectos y entrar en uno.
Description: React + Vite + Tailwind. Cliente de API centralizado en un módulo, generado desde `openapi.yaml`. Dashboard con una tarjeta por proyecto (estado + KPI principal) y vista de detalle con vídeo anotado, tres KPIs y dos gráficas temporales con selector de ventana. Estados de carga, vacío y error en todas las vistas. Tests con Vitest de la lógica de transformación de datos.

## 14. Proyecto 01: parking y tráfico

Goal: el primer prototipo corriendo de punta a punta sobre el directo real.
Description: `projects/01-parking-trafico/` con su `project.yaml` y su `zones.json` dibujado sobre un fotograma real del directo, la línea de conteo colocada, y las tasas de muestreo ajustadas a los fps medidos en la tarea 6. Documentar en el README qué mide cada métrica y cuál es su fiabilidad.

## 15. Docker Compose

Goal: `docker compose up` levanta API, frontend, base de datos y un worker.
Description: Un servicio por bloque, imagen del worker con las dependencias de visión, Postgres para el entorno contenedorizado y volumen para los pesos de los modelos. Instrucciones reproducibles desde cero en el README.

---

## Después del MVP

## 16. Editor visual de zonas

Goal: dibujo los polígonos de las plazas en el navegador en vez de a mano.
Description: Vista que carga un snapshot actual del proyecto, permite dibujar y editar polígonos, y guarda `zones.json`. El worker recarga la configuración sin reiniciarse.

## 17. Aceleración con OpenVINO

Goal: más fps en la misma máquina sin comprar nada.
Description: Exportar YOLO11n a OpenVINO, añadir el detector `openvino_yolo`, y medir contra el detector de PyTorch sobre el mismo vídeo. Se acepta solo si la mejora es medible y la salida coincide dentro de una tolerancia.
