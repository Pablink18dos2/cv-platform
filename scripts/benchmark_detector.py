"""Compara configuraciones del detector sobre los mismos fotogramas.

Corrige el defecto de metodo del spike inicial (`spike_yolo_perf.py`), que tomo sus dos
tablas en momentos distintos del directo, con trafico distinto, y por eso solo daba una
comparacion indicativa. Aqui todas las configuraciones ven **exactamente los mismos
fotogramas** del fixture, asi que las diferencias son atribuibles a la configuracion.

    uv run python scripts/benchmark_detector.py
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from cvp.core import BBox, Frame
from cvp.detectors import YoloDetector
from cvp.sources import FileVideoSource

RAIZ = Path(__file__).resolve().parent.parent
FIXTURE = RAIZ / "tests" / "fixtures" / "video" / "calle-1080p-6s.mp4"
CLASES = ["car", "motorcycle", "bus", "truck"]

# La region util de esta escena: sin el cielo, sin las fachadas y sin el fondo lejano, donde
# los coches son manchas de pocos pixeles que se tapan entre si. Ver
# projects/01-parking-trafico/fuente.md.
ROI = BBox(288, 302, 1920, 1080)

CONFIGURACIONES = [
    ("completo 640", 640, None),
    ("completo 960", 960, None),
    ("completo 1280", 1280, None),
    ("roi 640", 640, ROI),
    ("roi 960", 960, ROI),
]


def cargar(n: int) -> list[Frame]:
    origen = FileVideoSource(FIXTURE, target_fps=30)
    origen.open()
    try:
        return [f for f, _ in zip(origen.frames(), range(n), strict=False)]
    finally:
        origen.close()


def medir(detector: YoloDetector, fotogramas: list[Frame], roi: BBox | None) -> dict:
    detector.detect(fotogramas[0], roi)  # calentamiento

    latencias: list[float] = []
    por_clase: dict[str, int] = {}
    total = 0
    for fotograma in fotogramas:
        inicio = time.perf_counter()
        detecciones = detector.detect(fotograma, roi)
        latencias.append((time.perf_counter() - inicio) * 1000)
        total += len(detecciones)
        for d in detecciones:
            por_clase[d.label] = por_clase.get(d.label, 0) + 1

    mediana = statistics.median(latencias)
    return {
        "mediana_ms": mediana,
        "p95_ms": sorted(latencias)[int(0.95 * len(latencias)) - 1],
        "fps": 1000 / mediana,
        "por_fotograma": total / len(fotogramas),
        "por_clase": por_clase,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--conf", type=float, default=0.3)
    args = parser.parse_args()

    print(f"Cargando {args.frames} fotogramas de {FIXTURE.name} ...")
    fotogramas = cargar(args.frames)
    alto, ancho = fotogramas[0].image.shape[:2]
    print(f"  {len(fotogramas)} fotogramas de {ancho}x{alto}")
    print("  todas las configuraciones ven exactamente estos mismos fotogramas\n")

    filas = []
    for nombre, imgsz, roi in CONFIGURACIONES:
        detector = YoloDetector("yolo11n.pt", imgsz=imgsz, conf=args.conf, classes=CLASES)
        filas.append((nombre, medir(detector, fotogramas, roi)))

    print(f"{'configuracion':>16} {'mediana':>9} {'p95':>8} {'fps':>7} {'veh/frame':>10}")
    print("-" * 56)
    for nombre, r in filas:
        print(
            f"{nombre:>16} {r['mediana_ms']:>8.0f}ms {r['p95_ms']:>7.0f}ms "
            f"{r['fps']:>7.1f} {r['por_fotograma']:>10.1f}"
        )

    print("\nPor clase (el desglose que esta escena no permite fiarse):")
    for nombre, r in filas:
        print(f"  {nombre:>16}: {r['por_clase']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
