# CV Platform - Plan

> Product specification. It is the source of truth for **what** we build and **for whom**.
> If an acceptance criterion is wrong, fix it here and in the issue, never in the code.

## What this is

A local computer-vision platform for building and exploring video-analysis prototypes at zero
cloud cost. A **project** is a configuration file that connects a video source (connector) to a
detection model and one or more business analyzers. All projects share the same front end: a
dashboard from which you navigate to each project's view.

The real goal of this first version is not a sellable product: it is to validate that the
**block architecture** can take a new connector, a new model or a new analysis without touching
the rest of the system.

## Users

- **Platform operator** (me) - defines projects in YAML, draws the zones of interest on a frame,
  starts and stops workers, and checks why a project went down.
- **Observer** - opens the dashboard, enters a project and reads the annotated video and the
  metrics. Configures nothing, starts nothing, cannot break anything.

In the MVP both roles are the same person and there is no authentication. The separation is
here because it shapes the API design: everything the observer needs is read-only.

## The main flow

1. I open the dashboard and see the list of projects, each with its status (running / stopped /
   down) and its main KPI.
2. I enter the *Parking and traffic* project.
3. I see the annotated video almost live: vehicle boxes, their tracking ID, the parking zones
   colored by free or occupied, and the counting line.
4. Above the video, three KPIs: **free spaces / total**, **vehicles per minute** and
   **dominant direction**.
5. Below, two time-series charts - parking occupancy and traffic flow - with a window selector
   (last hour / last 24 h).
6. If the camera framing changes, I open the zone editor, redraw the polygons on a current
   frame, save, and the worker reloads the configuration without restarting.

## Features

**MVP**

- Interchangeable video connectors: local file, YouTube live stream, RTSP, webcam.
- Object detection with YOLO11 (pretrained COCO weights), replaceable by another detector.
- Multi-object tracking to give each vehicle a stable identity across frames.
- **Traffic** analyzer: directional line crossings, per-minute counts and vehicle density in
  the scene.
- **Parking** analyzer: occupancy per space defined as a polygon, with temporal hysteresis so
  that occupancy does not flicker.
- Persistence of metrics and events, and per-minute aggregation.
- Read-only API + streaming of the annotated video.
- Dashboard with a project list and a detail view shared by all projects.

**After the MVP**

- Visual zone editor in the browser.
- OpenVINO acceleration on the Intel iGPU.
- More analyzers (average dwell time per space, queues, heat map).

## Non-goals

What this project does **not** do. This list is worth as much as the feature list: it is what
keeps the system from growing sideways.

- **It does not read license plates or identify people.** Neither in the MVP nor later. The
  source is public and the platform counts objects, it does not follow individuals.
- It does not do re-identification of a vehicle across different cameras.
- It does not train its own models. Pretrained weights are used; fine-tuning is another project.
- It does not use paid cloud inference. Everything runs on the laptop.
- It has no multi-user support, login or permissions.
- It does not send alerts or notifications.
- **It does not automatically detect where the parking spaces are.** They are drawn by hand.
  Inferring parking geometry from video is an open problem and not this project.
- It does not guarantee strict real time. We sample frames; we do not process all 25 fps.

## Hardware constraints

They shape the whole design, so they are in the spec and not in a footnote:

| | |
|---|---|
| CPU | Intel i7-1365U (2 P-cores + 8 E-cores, 15 W) |
| GPU | Intel Iris Xe integrated - **no CUDA** |
| RAM | 32 GB |

Design consequences, not opinions:

- **Nano** model (YOLO11n) by default. The s/m/l sizes can be configured, but the default
  configuration has to run smoothly on this machine.
- The inference rate is **decoupled** from the video rate. Each analyzer declares the frequency
  it needs: ~5 fps for traffic (the minimum for the tracker to associate IDs), ~0.5 fps for
  parking (a space does not change in two seconds).
- The inference worker is a **separate** process from the API. This is what will later allow
  deploying the API and front end on a free cloud plan while the heavy work stays at home.

## Acceptance criteria (MVP)

- [ ] I can start a worker pointing at a project file and it processes the YouTube live stream
      for 30 minutes in a row without intervention.
- [ ] If the live stream URL expires mid-run, the worker resolves it again and continues,
      leaving a record in the log.
- [ ] The dashboard lists the existing projects and I can enter one from there.
- [ ] The project view shows the annotated video with one box and one ID per vehicle.
- [ ] The view shows "free spaces / total" and the number goes down when a car parks in a
      defined zone.
- [ ] The view shows vehicles per minute broken down by direction of travel.
- [ ] There is a chart of parking occupancy for the last 2 hours.
- [ ] The analyzers have tests that run on synthetic detections, with no video and no network.
- [ ] The whole system starts with `docker compose up`.
- [ ] `uv run pytest` passes and runs in CI on every push.

## Tech stack

Decided in Phase 1. Each choice includes why, including "because I can review it".

| Block | Decision | Why |
|---|---|---|
| Language | Python 3.13 + `uv` | That is where the whole vision ecosystem lives. `uv` is already installed and resolves fast. |
| Detection | `ultralytics` (YOLO11) | The de facto standard, maintained, and it ships a tracker and ONNX/OpenVINO export in the same library. |
| Tracking | ByteTrack (via ultralytics) | Included, adds no dependency, and works well with noisy detections. |
| Geometry / annotation | `supervision` | Polygons, counting lines and annotation drawing already solved and tested. |
| Video | OpenCV + `yt-dlp` + ffmpeg | `yt-dlp` resolves the live stream's HLS; OpenCV/ffmpeg decode. ffmpeg is already on the system. |
| API | FastAPI + Uvicorn | Generates the `openapi.yaml` that is the contract with the front end, instead of writing it by hand and letting it drift. |
| Persistence | SQLite (local) / Postgres (Docker) via SQLModel | A single model layer for both environments. SQLite in WAL mode easily handles this volume. |
| Frontend | React + Vite + Tailwind | Front end shared by all projects, with backend communication centralized in a client generated from the OpenAPI. |
| Tests | pytest + Vitest | - |

**Rejected:**

- **Streamlit** - starts up in an afternoon, but it does not support a multi-project dashboard
  or annotated-video streaming without fighting the framework.
- **Next.js** - SSR and a Node runtime in production that an internal dashboard does not need.
- **Django** - the ORM and admin add nothing here, and the project is not CRUD.
- **Kafka / Redis Streams** between worker and API - infrastructure this volume does not
  justify. The database is the queue. If it is ever needed, the `Sink` block is where to change it.
- **CUDA / GPU cloud** - explicit cost constraint.

## Known risks

Not excuses in advance: these are the things to measure early because they could invalidate
the approach.

1. **The live stream URL expires.** The HLS URLs that `yt-dlp` returns live for hours. The
   connector has to re-resolve, not die. It is the number one reason these prototypes show up
   down in the morning.
2. **CPU performance.** The real fps of YOLO11n at 640 px on this machine must be measured
   before accepting the sampling rate. If they do not reach it, the way out is OpenVINO or a
   lower resolution, not a smaller scope.
3. **Occlusion and perspective.** A car parked behind another may not be seen. Measured
   occupancy is an approximation; its reliability has to be stated, not presented as exact.
4. **Night and rain.** COCO weights degrade considerably in low light. Look at a night's
   metric before drawing conclusions from the time series.
5. **The camera moves.** If the live stream operator changes the framing, all polygons stop
   being valid. Hence the zone editor and hot reload are in the plan.

## Open questions

- ~~Concrete URL of the first prototype's YouTube live stream.~~ Resolved: *Town of Collingwood
  Live Stream*, 1080p30. The source reconnaissance is in
  [`projects/01-parking-trafico/fuente.md`](../projects/01-parking-trafico/fuente.md) (Spanish).
- Are individual detections stored, or only per-minute aggregates? We start with aggregates and
  a sample of detections; revisit when the database is seen to grow.
- Where the API/front end is deployed when the time comes (the Deployment criterion of the
  course checklist, [`checklist-proyecto-final.md`](checklist-proyecto-final.md)).
