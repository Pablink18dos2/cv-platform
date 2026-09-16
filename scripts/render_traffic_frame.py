"""Renderiza un fotograma anotado por el analizador de trafico sobre el fixture de 1080p.

Es la comprobacion visual de #10 —la linea donde debe, los contadores legibles, los puntos
de referencia sobre el asfalto y no en el aire— y la herramienta con la que #14 colocara
la linea real del proyecto 01 con el fotograma delante: se cambia la linea, se vuelve a
renderizar y se mira.

    uv run python scripts/render_traffic_frame.py data/traffic_flow_anotado.jpg
    uv run python scripts/render_traffic_frame.py salida.jpg --frame 20 --line 0.25,0.62,0.62,0.50

Corre detector y tracker de verdad (`track_activation_threshold: 0.5`, lo que #7 midio que
hace falta en esta escena) sobre todos los fotogramas hasta el pedido, para que los
contadores de la imagen sean los acumulados hasta ese momento, e imprime cada cruce.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from cvp.analyzers import TrafficFlowAnalyzer
from cvp.analyzers.geometry import LineaDeConteo
from cvp.detectors import YoloDetector
from cvp.sources import FileVideoSource
from cvp.trackers import ByteTrackTracker

RAIZ = Path(__file__).resolve().parent.parent
FIXTURE = RAIZ / "tests" / "fixtures" / "video" / "calle-1080p-6s.mp4"
CLASES = ["car", "motorcycle", "bus", "truck"]

# La misma linea que el test `slow` de `tests/unit/test_analyzer_traffic.py`: cruza la
# calzada a media altura, y su lado positivo es el de los vehiculos que se acercan.
LINEA_POR_DEFECTO = "0.25,0.62,0.62,0.50"


def _linea(texto: str) -> LineaDeConteo:
    x1, y1, x2, y2 = (float(v) for v in texto.split(","))
    return LineaDeConteo(
        id="calzada",
        p1=(x1, y1),
        p2=(x2, y2),
        positive_direction="acercandose",
        negative_direction="alejandose",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("salida", type=Path, help="ruta del JPEG anotado")
    parser.add_argument(
        "--frame", type=int, default=-1, help="indice del fotograma; -1 = el ultimo"
    )
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--line", default=LINEA_POR_DEFECTO, help="x1,y1,x2,y2 en fracciones")
    args = parser.parse_args()

    detector = YoloDetector("yolo11n.pt", imgsz=640, conf=0.35, classes=CLASES)
    tracker = ByteTrackTracker(frame_rate=args.fps, track_activation_threshold=0.5)
    analizador = TrafficFlowAnalyzer([_linea(args.line)], name="trafico")

    origen = FileVideoSource(FIXTURE, target_fps=args.fps)
    origen.open()
    try:
        ultimo = None
        for fotograma in origen.frames():
            detecciones = detector.detect(fotograma)
            tracks = tracker.update(fotograma, detecciones)
            salida = analizador.process(fotograma, tracks, detecciones)
            for evento in salida.events:
                print(f"fotograma {fotograma.index:3d}: {dict(evento.attributes)}")
            ultimo = (fotograma, tracks)
            if fotograma.index == args.frame:
                break
    finally:
        origen.close()

    if ultimo is None:
        raise SystemExit("el fixture no ha dado ningun fotograma")
    fotograma, tracks = ultimo
    anotado = analizador.annotate(fotograma.image, tracks)
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.salida), anotado)
    print(f"fotograma {fotograma.index} con {len(tracks)} tracks -> {args.salida.resolve()}")
    print(f"contadores: {dict(analizador.contadores)}")


if __name__ == "__main__":
    main()
