"""Genera el clip de la demo desplegada: video real de Collingwood anotado + linea de tiempo.

La demo de Render es ligera (sin detector), asi que no analiza nada en vivo: reproduce en
bucle un fragmento real, ya analizado **offline** con el pipeline del proyecto 01 (mismo
detector, tracker y analizadores `plazas` y `trafico` del `project.yaml`, a 5 fps; el
`framing_guard` no entra porque no aporta nada al clip).

    # 1. Grabar ~12 min del directo (el script no lo hace; el fichero queda fuera del repo)
    yt-dlp --extractor-args "youtube:player_client=web_safari,tv,android" -f 96 -g \\
        https://www.youtube.com/watch?v=EPKWu223XEg        # imprime la URL HLS
    ffmpeg -i "<URL HLS>" -t 720 -c copy collingwood.mp4

    # 2. Analizar, elegir ventana y generar deploy/demo/{clip.mp4,timeline.json}
    uv run python scripts/make_demo_clip.py collingwood.mp4 --start 120 --duration 150
    uv run python scripts/make_demo_clip.py collingwood.mp4          # ventana mas activa

Sin `--start`, se elige la ventana de `--duration` s con mas cambios de plazas (desempate:
mas cruces de la linea). **Es un fragmento elegido por actividad, no una muestra
representativa**: la demo lo dice asi.

Cada fotograma n del mp4 es el fotograma de analisis `start + n` (5 fps), asi que el
fotograma n corresponde a t = n / fps en `timeline.json`. Los contadores de cruces dibujados
cuentan desde el inicio del clip, igual que la serie. Los analizadores corren desde el
principio de la grabacion (la histeresis de las plazas necesita calentarse) y solo se dibuja
dentro de la ventana.

Necesita ffmpeg: el del sistema o, sin anadir dependencias, `uv run --with imageio-ffmpeg ...`.
Detectar cuesta unos minutos en CPU; `--cache` guarda detecciones y tracks para repetir la
eleccion de ventana sin volver a detectar.
"""

from __future__ import annotations

import argparse
import json
import pickle
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from cvp.core import BBox, Frame
from cvp.pipeline import load_project_config
from cvp.pipeline.config import FileSource
from cvp.pipeline.factory import build_pipeline

RAIZ = Path(__file__).resolve().parent.parent
PROYECTO = RAIZ / "projects" / "01-parking-trafico" / "project.yaml"
DESTINO = RAIZ / "deploy" / "demo"
# Los primeros segundos de la grabacion son la histeresis calentandose: no se eligen.
CALENTAMIENTO_S = 30


@dataclass
class Registro:
    """Lo que dicen los analizadores en un fotograma de analisis."""

    libres: int | None = None
    vehiculos: int | None = None
    cruces: list[str] = field(default_factory=list)  # sentidos
    eventos: list[tuple[str, str]] = field(default_factory=list)  # (nombre, plaza)


def ffmpeg_exe() -> str:
    """El ffmpeg del sistema o, si no hay, el de `imageio-ffmpeg` (`uv run --with`)."""
    if exe := shutil.which("ffmpeg"):
        return exe
    try:
        import imageio_ffmpeg
    except ImportError:
        raise SystemExit(
            "no hay ffmpeg: instalalo o ejecuta con `uv run --with imageio-ffmpeg ...`"
        ) from None
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def construir(video: Path, proyecto: Path):
    """Los bloques del proyecto 01 con el video como fuente y sin sinks."""
    cfg = load_project_config(proyecto)
    fuente = FileSource(type="file", path=video, target_fps=cfg.source.target_fps)
    analizadores = [a for a in cfg.analyzers if a.type != "framing_guard"]
    cfg = cfg.model_copy(update={"source": fuente, "sinks": [], "analyzers": analizadores})
    return cfg, build_pipeline(cfg)


def detectar(video: Path, proyecto: Path, cache: Path | None):
    """Detecta y sigue sobre toda la grabacion. Devuelve `(fotogramas, (ancho, alto), fps)`.

    Un fotograma es `(indice, timestamp, detecciones, tracks)`. Es la parte cara.
    """
    if cache is not None and cache.exists():
        print(f"detecciones desde la cache {cache}", file=sys.stderr)
        return pickle.loads(cache.read_bytes())
    cfg, bloques = construir(video, proyecto)
    fuente, detector, tracker = bloques.source, bloques.detector, bloques.tracker
    fuente.open()
    detector.warmup()
    ancho, alto = fuente.info.width, fuente.info.height
    roi = cfg.detector.roi.to_pixels(ancho, alto)
    fotogramas = []
    t0 = time.perf_counter()
    try:
        for frame in fuente.frames():
            detecciones = detector.detect(frame, roi)
            tracks = tracker.update(frame, detecciones)
            fotogramas.append((frame.index, frame.timestamp, detecciones, tracks))
            if len(fotogramas) % 250 == 0:
                print(
                    f"  {len(fotogramas)} fotogramas, {time.perf_counter() - t0:.0f} s",
                    file=sys.stderr,
                )
    finally:
        fuente.close()
    captura = cv2.VideoCapture(str(video))
    esperados = int(
        captura.get(cv2.CAP_PROP_FRAME_COUNT)
        * cfg.source.target_fps
        / captura.get(cv2.CAP_PROP_FPS)
    )
    captura.release()
    if len(fotogramas) < 0.95 * esperados:
        # OpenCV corta el video en silencio si se queda sin memoria: mejor parar que
        # analizar media grabacion.
        raise SystemExit(f"la lectura acabo en {len(fotogramas)} fotogramas, esperaba ~{esperados}")
    resultado = (fotogramas, (ancho, alto), cfg.source.target_fps)
    if cache is not None:
        cache.write_bytes(pickle.dumps(resultado))
    return resultado


def reproducir(fotogramas, tamano, video: Path, proyecto: Path, ventana=None, dibujar=None):
    """Pasa los fotogramas ya detectados por analizadores nuevos y devuelve un `Registro`
    por fotograma.

    Sin `ventana` no hay imagen (solo se necesita su forma). Con `ventana=(ini, fin)`
    se relee el video y se llama a `dibujar(n, imagen, tracks, detecciones, analizadores)`
    para los fotogramas `ini <= indice < fin`, con los contadores de cruces a cero en `ini`.
    """
    _, bloques = construir(video, proyecto)
    analizadores = bloques.analyzers
    ancho, alto = tamano
    en_blanco = np.zeros((alto, ancho, 3), dtype=np.uint8)
    imagenes = None
    if ventana is not None:
        bloques.source.open()
        imagenes = iter(bloques.source.frames())
    registros = []
    try:
        for indice, timestamp, detecciones, tracks in fotogramas:
            imagen = en_blanco
            if imagenes is not None:
                imagen = next(imagenes).image
            frame = Frame(image=imagen, index=indice, timestamp=timestamp)
            if ventana is not None and indice == ventana[0]:
                _poner_contadores_a_cero(analizadores)
            reg = Registro()
            for analizador in analizadores:
                if indice % analizador.every_n_frames:
                    continue
                salida = analizador.process(frame, tracks, detecciones)
                for e in salida.events:
                    if e.name in ("space_occupied", "space_freed"):
                        reg.eventos.append((e.name, str(e.attributes["zone"])))
                    elif e.name == "line_crossing":
                        reg.cruces.append(str(e.attributes["direction"]))
                for m in salida.metrics:
                    if m.name == "spaces_free":
                        reg.libres = int(m.value)
                    elif m.name == "vehicles_in_scene":
                        reg.vehiculos = int(m.value)
            registros.append(reg)
            if ventana is not None and ventana[0] <= indice < ventana[1]:
                dibujar(indice - ventana[0], imagen, tracks, detecciones, analizadores)
            if ventana is not None and indice >= ventana[1] - 1:
                break
    finally:
        if ventana is not None:
            bloques.source.close()
    return registros


def _poner_contadores_a_cero(analizadores) -> None:
    """Que el contador dibujado cuente desde el inicio del clip. Toca un atributo privado
    de `traffic_flow` a proposito: la alternativa es dibujar los contadores por nuestra
    cuenta y duplicar su rotulo."""
    for analizador in analizadores:
        contadores = getattr(analizador, "_contadores", None)
        if contadores is not None:
            for clave in contadores:
                contadores[clave] = 0


def elegir_ventana(registros, fps: int, duracion_s: int) -> int:
    """Indice de inicio (multiplo de `fps`) de la ventana con mas cambios de plazas."""
    n = duracion_s * fps
    primero = CALENTAMIENTO_S * fps
    ultimo = len(registros) - n
    if ultimo < primero:
        raise SystemExit(
            f"la grabacion ({len(registros) / fps:.0f} s) es demasiado corta para una ventana "
            f"de {duracion_s} s tras {CALENTAMIENTO_S} s de calentamiento"
        )
    mejor, mejor_puntuacion = primero, (-1, -1)
    for ini in range(primero, ultimo + 1, fps):
        ventana = registros[ini : ini + n]
        puntuacion = (
            sum(len(r.eventos) for r in ventana),
            sum(len(r.cruces) for r in ventana),
        )
        if puntuacion > mejor_puntuacion:
            mejor, mejor_puntuacion = ini, puntuacion
    return mejor


def escalar(bbox: BBox, f: float) -> BBox:
    return BBox(bbox.x1 * f, bbox.y1 * f, bbox.x2 * f, bbox.y2 * f)


def abrir_ffmpeg(destino: Path, ancho: int, alto: int, fps: int, crf: int):
    orden = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ancho}x{alto}", "-r", str(fps),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destino),
    ]  # fmt: skip
    return subprocess.Popen(orden, stdin=subprocess.PIPE)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("video", type=Path, help="grabacion del directo (fuera del repo)")
    p.add_argument("--start", type=float, help="inicio de la ventana en s; sin el, la mas activa")
    p.add_argument("--duration", type=int, default=150, help="duracion del clip en s")
    p.add_argument("--out-dir", type=Path, default=DESTINO)
    p.add_argument("--project", type=Path, default=PROYECTO)
    p.add_argument("--width", type=int, default=854, help="ancho del clip (alto proporcional)")
    p.add_argument("--crf", type=int, default=24, help="calidad x264 (menor = mejor y mas peso)")
    p.add_argument("--cache", type=Path, help="pickle con detecciones/tracks (se crea si falta)")
    p.add_argument("--recorded-at", help="inicio de la grabacion en UTC (ISO); defecto: mtime")
    args = p.parse_args()

    video = args.video.resolve()
    fotogramas, (ancho, alto), fps_f = detectar(video, args.project, args.cache)
    fps = int(fps_f)
    registros = reproducir(fotogramas, (ancho, alto), video, args.project)

    if args.start is None:
        ini = elegir_ventana(registros, fps, args.duration)
    else:
        ini = int(args.start * fps) // fps * fps  # alineado a segundo: las plazas miden cada 5
    n = args.duration * fps
    fin = ini + n
    if fin > len(registros):
        raise SystemExit(f"la ventana [{ini / fps:.0f}, {fin / fps:.0f}] s excede la grabacion")
    ventana_reg = registros[ini:fin]
    eventos_ventana = sum(len(r.eventos) for r in ventana_reg)
    print(
        f"ventana {ini / fps:.0f}-{fin / fps:.0f} s: {eventos_ventana} cambios de plaza, "
        f"{sum(len(r.cruces) for r in ventana_reg)} cruces",
        file=sys.stderr,
    )

    # --- Segunda pasada: se relee el video y se dibuja solo la ventana -------------------
    f = args.width / ancho
    w, h = args.width, round(alto * f)
    w, h = w - w % 2, h - h % 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    clip = args.out_dir / "clip.mp4"
    proc = abrir_ffmpeg(clip, w, h, fps, args.crf)
    escritos = 0

    def dibujar(_n, imagen, tracks, detecciones, analizadores) -> None:
        nonlocal escritos
        lienzo = cv2.resize(imagen, (w, h), interpolation=cv2.INTER_AREA)
        for d in detecciones:  # lo que mira `plazas`: la caja cruda, no el track
            b = escalar(d.bbox, f)
            cv2.rectangle(lienzo, (int(b.x1), int(b.y1)), (int(b.x2), int(b.y2)), (255, 220, 0), 1)
        tracks_pequenos = [replace(t, bbox=escalar(t.bbox, f)) for t in tracks]
        for analizador in analizadores:
            lienzo = analizador.annotate(lienzo, tracks_pequenos)
        proc.stdin.write(np.ascontiguousarray(lienzo).tobytes())
        escritos += 1

    reproducir(fotogramas, (ancho, alto), video, args.project, (ini, fin), dibujar)
    proc.stdin.close()
    if proc.wait() != 0 or escritos != n:
        raise SystemExit(f"ffmpeg ha fallado o faltan fotogramas ({escritos}/{n})")

    # --- timeline.json ------------------------------------------------------------------
    serie = []
    libres = ventana_reg[0].libres
    for s in range(args.duration):
        trozo = ventana_reg[s * fps : (s + 1) * fps]
        if trozo[0].libres is not None:
            libres = trozo[0].libres
        serie.append(
            {
                "t": float(s),
                "spaces_free": libres,
                "vehicles_in_scene": trozo[0].vehiculos or 0,
                "crossings": {
                    "hacia_camara": sum(c == "hacia_camara" for r in trozo for c in r.cruces),
                    "hacia_fondo": sum(c == "hacia_fondo" for r in trozo for c in r.cruces),
                },
            }
        )
    eventos = [
        {"t": round(i / fps, 3), "name": nombre, "zone": zona}
        for i, r in enumerate(ventana_reg)
        for nombre, zona in r.eventos
    ]
    zonas = json.loads((args.project.parent / "zones.json").read_text(encoding="utf-8"))
    total = len(zonas["zones"] if isinstance(zonas, dict) else zonas)
    grabado = (
        datetime.fromisoformat(args.recorded_at)
        if args.recorded_at
        else datetime.fromtimestamp(video.stat().st_mtime, UTC)
        - timedelta(seconds=len(registros) / fps)
    )
    inicio_clip = grabado + timedelta(seconds=ini / fps)
    timeline = {
        "version": 1,
        "source": (
            f"Collingwood live stream excerpt recorded {inicio_clip:%Y-%m-%d %H:%M} UTC, "
            f"analysed offline at {fps} fps"
        ),
        "duration_s": n / fps,
        "video": "/demo/clip.mp4",
        "spaces_total": total,
        "series": serie,
        "events": eventos,
    }
    (args.out_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(
        f"{clip} ({clip.stat().st_size / 1e6:.1f} MB, {n / fps:.0f} s, {w}x{h}); "
        f"{len(eventos)} eventos de plaza",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
