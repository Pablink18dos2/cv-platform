"""Renderiza un fotograma anotado por el analizador de aparcamiento sobre el fixture de 1080p.

Es la comprobacion visual de #11 —los poligonos sobre las bahias y no desplazados, el
color del estado, el resumen "N libres de M" legible— y la herramienta con la que #14
colocara las plazas reales del proyecto 01 con el fotograma delante: se edita el
`zones.json`, se vuelve a renderizar y se mira.

    uv run python scripts/render_parking_frame.py data/parking_occupancy_anotado.jpg
    uv run python scripts/render_parking_frame.py salida.jpg --zones projects/01/zones.json
    uv run python scripts/render_parking_frame.py salida.jpg --frame 10 --to-occupied 2 --cajas

Corre el detector de verdad (sin tracker: este analizador no lo usa) sobre todos los
fotogramas hasta el pedido, para que el estado de cada plaza sea el que la histeresis ha
confirmado hasta ese momento, e imprime por fotograma el mejor IoU de cada plaza y los
eventos. Con `--cajas` pinta ademas las cajas de las detecciones, que es lo que hace falta
para ver si un poligono esta donde el coche.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from cvp.analyzers import ParkingOccupancyAnalyzer
from cvp.analyzers.geometry import rasterizar_poligono
from cvp.analyzers.parking import cargar_zonas
from cvp.detectors import YoloDetector
from cvp.sources import FileVideoSource

RAIZ = Path(__file__).resolve().parent.parent
FIXTURE = RAIZ / "tests" / "fixtures" / "video" / "calle-1080p-6s.mp4"
CLASES = ["car", "motorcycle", "bus", "truck"]

# Las mismas tres bahias que el test `slow` de `tests/unit/test_analyzer_parking.py`.
ZONAS_POR_DEFECTO = RAIZ / "tests" / "fixtures" / "zones" / "calle-1080p-bahias.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("salida", type=Path, help="ruta del JPEG anotado")
    parser.add_argument("--zones", type=Path, default=ZONAS_POR_DEFECTO, help="zones.json")
    parser.add_argument(
        "--frame", type=int, default=-1, help="indice del fotograma; -1 = el ultimo"
    )
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--iou", type=float, default=0.35, help="occupied_iou")
    parser.add_argument("--to-occupied", type=float, default=2.0, help="to_occupied_s")
    parser.add_argument("--to-free", type=float, default=4.0, help="to_free_s")
    parser.add_argument("--cajas", action="store_true", help="pinta tambien las detecciones")
    args = parser.parse_args()

    detector = YoloDetector("yolo11n.pt", imgsz=640, conf=0.35, classes=CLASES)
    analizador = ParkingOccupancyAnalyzer(
        cargar_zonas(args.zones),
        name="plazas",
        occupied_iou=args.iou,
        to_occupied_s=args.to_occupied,
        to_free_s=args.to_free,
    )

    origen = FileVideoSource(FIXTURE, target_fps=args.fps)
    origen.open()
    try:
        ancho, alto = origen.info.width, origen.info.height
        mascaras = {
            z.id: rasterizar_poligono(z.to_pixels(ancho, alto), ancho, alto)
            for z in analizador.zones
        }
        ultimo = None
        for fotograma in origen.frames():
            detecciones = detector.detect(fotograma)
            salida = analizador.process(fotograma, [], detecciones)
            mejores = " ".join(
                f"{z.id}={max((m.iou_con(d.bbox) for d in detecciones), default=0.0):.2f}"
                f"/{analizador.estado_de(z.id).value}"
                for z, m in ((z, mascaras[z.id]) for z in analizador.zones)
            )
            print(f"fotograma {fotograma.index:3d}: {len(detecciones):2d} detecciones  {mejores}")
            for evento in salida.events:
                print(f"    {evento.name}: {dict(evento.attributes)}")
            ultimo = (fotograma, detecciones)
            if fotograma.index == args.frame:
                break
    finally:
        origen.close()

    if ultimo is None:
        raise SystemExit("el fixture no ha dado ningun fotograma")
    fotograma, detecciones = ultimo
    anotado = analizador.annotate(fotograma.image, [])
    if args.cajas:
        for d in detecciones:
            b = d.bbox
            cv2.rectangle(anotado, (int(b.x1), int(b.y1)), (int(b.x2), int(b.y2)), (255, 220, 0), 2)
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.salida), anotado)
    print(f"fotograma {fotograma.index}: {analizador.resumen()} -> {args.salida.resolve()}")


if __name__ == "__main__":
    main()
