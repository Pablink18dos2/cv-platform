# CV Platform

Plataforma local de visión por computador para montar prototipos de análisis de vídeo sin coste
de cloud. Un **proyecto** conecta una fuente de vídeo con un modelo de detección y uno o varios
analizadores de negocio, y todos los proyectos comparten el mismo frontal.

Toda la inferencia corre en local. No hace falta GPU ni cuenta en ningún servicio de pago.

## El problema

Analizar vídeo en directo (una calle, un aparcamiento, una cámara de proceso) normalmente acaba
en un script de usar y tirar por cada caso: uno para contar coches, otro para mirar plazas
libres, cada uno con su propia forma de leer el vídeo y de guardar resultados. Cuando llega el
tercer caso ya no se reutiliza nada.

Aquí la pieza que se reutiliza es la **plataforma**: connectors, modelos, analizadores y sinks
son bloques intercambiables detrás de un contrato. Añadir un caso nuevo es escribir un
analizador y un YAML, no un proyecto nuevo.

## Qué hace

- Conecta fuentes de vídeo intercambiables: fichero, directo de YouTube, RTSP, webcam.
- Detecta objetos con YOLO11 y les da identidad estable con seguimiento multiobjeto.
- Ejecuta analizadores sobre esas detecciones. Los dos primeros: **flujo de tráfico**
  (cruces de línea con sentido) y **ocupación de aparcamiento** (plazas libres por zona).
- Guarda eventos y métricas agregadas por minuto.
- Sirve un dashboard web con la lista de proyectos y, por cada uno, el vídeo anotado, sus KPIs
  y sus series temporales.

Lo que **no** hace está en [`_docs/plan.md`](_docs/plan.md#non-goals), y es igual de importante.
En resumen: ni matrículas, ni identificación de personas, ni entrenamiento de modelos.

## Arranque rápido

Requisitos: Python 3.13, [uv](https://docs.astral.sh/uv/) y ffmpeg en el PATH.

```bash
uv sync                 # instala dependencias en .venv
uv run pytest           # la suite completa
uv run ruff check .     # lint
uv run cvp --version    # el CLI responde
```

## Estructura

```
backend/src/cvp/   el paquete: core, sources, detectors, trackers, analyzers, sinks, pipeline, api
projects/          un directorio por proyecto: project.yaml + zones.json
frontend/          dashboard React compartido por todos los proyectos
tests/             unit (rápidos) · component (pipeline con dobles) · integration (modelo real)
_docs/             especificación, arquitectura y guías de trabajo
```

## Documentación

| Documento | Para qué |
|---|---|
| [`_docs/plan.md`](_docs/plan.md) | Qué se construye, para quién, y qué queda fuera |
| [`_docs/architecture.md`](_docs/architecture.md) | Los bloques, sus contratos y cómo se añade uno nuevo |
| [`_docs/process.md`](_docs/process.md) | Cómo se organiza el trabajo |
| [`_docs/testing-guidelines.md`](_docs/testing-guidelines.md) | Qué se testea y cómo |
| [`AGENTS.md`](AGENTS.md) | Contexto durable para agentes de código |

## Estado

En construcción. El backlog está en los issues de GitHub; el histórico inicial, en
[`_docs/tasks.md`](_docs/tasks.md).
