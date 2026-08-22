"""Spike: cuanto rinde YOLO11n en esta maquina y que ve de verdad en esta escena.

No forma parte del pipeline y no tiene tests. Es una medicion para fijar `imgsz` y las
tasas de muestreo con un dato delante en vez de estimarlas. Responde al riesgo 2 de
`_docs/plan.md` y alimenta los issues 6, 8, 10 y 11.

    uv run python scripts/spike_yolo_perf.py

Diseno de la medicion: primero se capturan N fotogramas a memoria y despues se cronometra
la inferencia sobre esa lista. Medir mientras se lee del directo daria los fps del stream
(30, porque llega en tiempo real), no los de la maquina.
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO
from yt_dlp import YoutubeDL

# Sin estos clientes, este directo concreto responde "This video is not available".
# Ver projects/01-parking-trafico/fuente.md.
CLIENTES_YOUTUBE = ["web_safari", "tv", "android"]

# COCO: los vehiculos y nada mas. Las personas se quedan fuera a proposito.
CLASES_VEHICULO = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

URL_POR_DEFECTO = "https://www.youtube.com/watch?v=EPKWu223XEg"
RAIZ = Path(__file__).resolve().parent.parent


def resolver_hls(url: str, formato: str = "96") -> str:
    """Devuelve la URL HLS directa del directo."""
    opciones = {
        "quiet": True,
        "no_warnings": True,
        "format": formato,
        "extractor_args": {"youtube": {"player_client": CLIENTES_YOUTUBE}},
    }
    with YoutubeDL(opciones) as ydl:
        info = ydl.extract_info(url, download=False)
    return info["url"]


def capturar(url_hls: str, n: int) -> list[np.ndarray]:
    """Lee n fotogramas a memoria."""
    captura = cv2.VideoCapture(url_hls)
    if not captura.isOpened():
        raise RuntimeError("cv2 no pudo abrir el stream")
    fotogramas = []
    try:
        while len(fotogramas) < n:
            ok, fotograma = captura.read()
            if not ok:
                break
            fotogramas.append(fotograma)
    finally:
        captura.release()
    if not fotogramas:
        raise RuntimeError("el stream no devolvio ningun fotograma")
    return fotogramas


def medir(modelo: YOLO, fotogramas: list[np.ndarray], imgsz: int, conf: float) -> dict:
    """Cronometra la inferencia y resume que se detecto."""
    # Calentamiento: la primera pasada incluye la construccion de los kernels.
    modelo.predict(fotogramas[0], imgsz=imgsz, conf=conf, verbose=False)

    latencias_ms: list[float] = []
    por_clase: dict[str, int] = {}
    por_banda = {"fondo": 0, "medio": 0, "primer plano": 0}
    areas: list[float] = []

    alto = fotogramas[0].shape[0]
    for fotograma in fotogramas:
        inicio = time.perf_counter()
        resultado = modelo.predict(
            fotograma, imgsz=imgsz, conf=conf, classes=list(CLASES_VEHICULO), verbose=False
        )[0]
        latencias_ms.append((time.perf_counter() - inicio) * 1000)

        for caja in resultado.boxes:
            x1, y1, x2, y2 = caja.xyxy[0].tolist()
            etiqueta = CLASES_VEHICULO[int(caja.cls)]
            por_clase[etiqueta] = por_clase.get(etiqueta, 0) + 1
            areas.append((x2 - x1) * (y2 - y1))
            centro_y = (y1 + y2) / 2
            if centro_y < alto / 3:
                por_banda["fondo"] += 1
            elif centro_y < 2 * alto / 3:
                por_banda["medio"] += 1
            else:
                por_banda["primer plano"] += 1

    n = len(fotogramas)
    return {
        "imgsz": imgsz,
        "mediana_ms": statistics.median(latencias_ms),
        "p95_ms": sorted(latencias_ms)[int(0.95 * len(latencias_ms)) - 1],
        "fps": 1000 / statistics.median(latencias_ms),
        "vehiculos_por_fotograma": sum(por_clase.values()) / n,
        "por_clase": por_clase,
        "por_banda": {k: round(v / n, 1) for k, v in por_banda.items()},
        "area_mediana_px": round(statistics.median(areas)) if areas else 0,
        "area_minima_px": round(min(areas)) if areas else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL_POR_DEFECTO)
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--imgsz", type=int, nargs="+", default=[640, 960, 1280])
    parser.add_argument("--conf", type=float, default=0.3)
    parser.add_argument(
        "--crop",
        type=float,
        nargs=4,
        metavar=("X1", "Y1", "X2", "Y2"),
        help="Recorta a esta region (fracciones 0-1) antes de inferir. Sube la resolucion "
        "efectiva sobre la zona util sin pagar el coste de un imgsz mayor.",
    )
    args = parser.parse_args()

    pesos = RAIZ / "models" / "yolo11n.pt"
    pesos.parent.mkdir(exist_ok=True)

    print(f"Resolviendo {args.url} ...")
    url_hls = resolver_hls(args.url)
    print(f"Capturando {args.frames} fotogramas ...")
    fotogramas = capturar(url_hls, args.frames)
    alto, ancho = fotogramas[0].shape[:2]
    print(f"  {len(fotogramas)} fotogramas de {ancho}x{alto}")

    if args.crop:
        x1, y1, x2, y2 = args.crop
        recorte = (
            slice(int(y1 * alto), int(y2 * alto)),
            slice(int(x1 * ancho), int(x2 * ancho)),
        )
        fotogramas = [f[recorte] for f in fotogramas]
        alto, ancho = fotogramas[0].shape[:2]
        print(f"  recortado a {ancho}x{alto}")
    print()

    modelo = YOLO(str(pesos))

    resultados = [medir(modelo, fotogramas, imgsz, args.conf) for imgsz in args.imgsz]

    print(f"{'imgsz':>6} {'mediana':>9} {'p95':>8} {'fps':>7} {'veh/frame':>10}")
    print("-" * 45)
    for r in resultados:
        print(
            f"{r['imgsz']:>6} {r['mediana_ms']:>8.0f}ms {r['p95_ms']:>7.0f}ms "
            f"{r['fps']:>7.1f} {r['vehiculos_por_fotograma']:>10.1f}"
        )

    print("\nQue ve en cada franja de la escena (vehiculos por fotograma):")
    print(f"{'imgsz':>6} {'fondo':>8} {'medio':>8} {'1er plano':>11} {'area min px':>13}")
    print("-" * 50)
    for r in resultados:
        b = r["por_banda"]
        print(
            f"{r['imgsz']:>6} {b['fondo']:>8} {b['medio']:>8} {b['primer plano']:>11} "
            f"{r['area_minima_px']:>13}"
        )

    print("\nPor clase:")
    for r in resultados:
        print(f"  imgsz {r['imgsz']}: {r['por_clase']}")

    # Un fotograma anotado por tamano, para poder mirarlos y no fiarse solo de los numeros.
    salida = RAIZ / "models" / "spike"
    salida.mkdir(exist_ok=True)
    for imgsz in args.imgsz:
        resultado = modelo.predict(
            fotogramas[-1],
            imgsz=imgsz,
            conf=args.conf,
            classes=list(CLASES_VEHICULO),
            verbose=False,
        )[0]
        cv2.imwrite(str(salida / f"anotado_{imgsz}.jpg"), resultado.plot())
    print(f"\nFotogramas anotados en {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
