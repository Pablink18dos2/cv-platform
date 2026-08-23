"""Detector YOLO sobre ultralytics.

**Nada del formato de ultralytics sale de este modulo.** Los `Results`, los `Boxes` y los
tensores de torch se quedan aqui dentro y lo que cruza la frontera son `Detection` del core.
Es lo que permitira cambiar a OpenVINO, a RT-DETR o a un modelo propio sin tocar una linea de
los analizadores.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from cvp.core import BBox, Detection, Frame
from cvp.core.registry import DETECTORS
from cvp.detectors.errors import DetectorError

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import YoloDetector as YoloDetectorConfig

_MODELS_DIR_POR_DEFECTO = "models"


@DETECTORS.register("yolo")
class YoloDetector:
    """Envuelve un modelo YOLO de ultralytics y devuelve `Detection` normalizadas.

    El modelo se carga **al construir**, no en la primera inferencia: asi un nombre de clase
    que no existe en el modelo falla al arrancar el worker y no cuarenta minutos despues.
    Para los tests, `modelo` se puede inyectar y entonces no se carga nada.
    """

    def __init__(
        self,
        weights: str = "yolo11n.pt",
        *,
        imgsz: int = 640,
        conf: float = 0.35,
        classes: list[str] | None = None,
        device: str = "cpu",
        models_dir: Path | str | None = None,
        modelo: Any | None = None,
    ) -> None:
        self.imgsz = imgsz
        self.conf = conf
        self.device = device
        self.models_dir = Path(
            models_dir
            if models_dir is not None
            else os.environ.get("CVP_MODELS_DIR", _MODELS_DIR_POR_DEFECTO)
        )
        self.weights = weights
        self._modelo = modelo if modelo is not None else self._cargar(weights)

        nombres = dict(self._modelo.names)
        self._nombre_de_id = {int(k): str(v) for k, v in nombres.items()}
        self._id_de_nombre = {v: k for k, v in self._nombre_de_id.items()}
        self.classes = list(classes) if classes else None
        self._ids_filtrados = self._traducir_clases(self.classes)

    # --- construccion -----------------------------------------------------------------

    def _cargar(self, weights: str) -> Any:
        from ultralytics import YOLO

        ruta = self._resolver_pesos(weights)
        try:
            return YOLO(str(ruta))
        except Exception as exc:  # ultralytics lanza de todo
            raise DetectorError(
                f"no se ha podido cargar el modelo {weights!r}: se buscó en {ruta} y "
                f"tampoco se ha podido descargar ({exc})"
            ) from exc

    def _resolver_pesos(self, weights: str) -> Path:
        """Un nombre suelto se busca (y si hace falta se descarga) en `models_dir`.

        Una ruta explicita se respeta tal cual y tiene que existir: si alguien escribe una
        ruta y nos la saltamos para descargar otra cosa, el YAML deja de decir la verdad.
        """
        ruta = Path(weights)
        if ruta.is_absolute() or ruta.parent != Path("."):
            if not ruta.exists():
                raise DetectorError(f"el fichero de pesos no existe: {ruta.resolve()}")
            return ruta
        self.models_dir.mkdir(parents=True, exist_ok=True)
        return self.models_dir / weights

    def _traducir_clases(self, classes: list[str] | None) -> list[int] | None:
        """Traduce nombres legibles a ids del modelo, o falla diciendo cuales hay."""
        if not classes:
            return None
        desconocidas = [c for c in classes if c not in self._id_de_nombre]
        if desconocidas:
            raise ValueError(
                f"el modelo no conoce {desconocidas}; sus clases son: {sorted(self._id_de_nombre)}"
            )
        return [self._id_de_nombre[c] for c in classes]

    @classmethod
    def from_config(cls, cfg: YoloDetectorConfig, **kwargs: Any) -> YoloDetector:
        """Construye desde el modelo de configuracion de #3.

        `cfg.roi` **no se usa aqui a proposito**. Viene en fracciones 0-1 y solo se puede
        pasar a pixeles cuando se sabe la resolucion de la fuente, que es despues de
        `source.open()`. Ademas el contrato de #2 dice que el ROI es un argumento por
        llamada y no estado del detector, para que dos analizadores puedan pedir regiones
        distintas. Quien lo traduce y lo pasa a `detect()` es el runner de #8.
        """
        return cls(
            cfg.weights,
            imgsz=cfg.imgsz,
            conf=cfg.conf,
            classes=list(cfg.classes),
            **kwargs,
        )

    # --- inferencia -------------------------------------------------------------------

    def warmup(self) -> None:
        """Una inferencia en vacio para que la primera de verdad no pague los kernels."""
        vacio = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
        self._predecir(vacio)

    def detect(self, frame: Frame, roi: BBox | None = None) -> list[Detection]:
        """Devuelve las detecciones del fotograma, siempre en coordenadas del original.

        Con `roi` se recorta antes de inferir. Medido sobre los mismos fotogramas
        (`_docs/rendimiento.md`), recortar a 640 detecta practicamente lo mismo que el
        fotograma completo a 960 (20,2 vehiculos frente a 20,4) con un 63 % mas de fps.
        **No** detecta mas que el fotograma completo a 1280, al contrario de lo que decia el
        spike inicial: 1280 ve mas, pero a 1,9 fps, que es demasiado lento para el tracker.

        Lo que hace que el recorte sea utilizable es que quien recorta traduce de vuelta: las
        cajas salen donde estan en el fotograma entero, no donde estaban en el recorte.
        """
        imagen = frame.image
        alto, ancho = imagen.shape[:2]

        if roi is None:
            recorte, dx, dy = imagen, 0.0, 0.0
        else:
            dentro = roi.clipped(ancho, alto)
            x1, y1 = int(dentro.x1), int(dentro.y1)
            x2, y2 = math.ceil(dentro.x2), math.ceil(dentro.y2)
            recorte = imagen[y1:y2, x1:x2]
            if recorte.size == 0:
                raise ValueError(f"el roi {roi} no deja ningun pixel del fotograma {ancho}x{alto}")
            dx, dy = float(x1), float(y1)

        return self._a_detecciones(self._predecir(recorte), dx, dy)

    def _predecir(self, imagen: np.ndarray) -> Any:
        return self._modelo.predict(
            imagen,
            imgsz=self.imgsz,
            conf=self.conf,
            classes=self._ids_filtrados,
            device=self.device,
            verbose=False,
        )

    def _a_detecciones(self, resultados: Any, dx: float, dy: float) -> list[Detection]:
        """Convierte la salida de ultralytics en `Detection`, y aqui acaba su formato.

        Todo se pasa por `float()` y `int()` a proposito: los tensores de torch de cero
        dimensiones se comparan como numeros y se imprimen como numeros, asi que un tensor
        colado en un `Detection` no se nota hasta que alguien intenta serializarlo.
        """
        if not resultados:
            return []
        detecciones: list[Detection] = []
        for caja in resultados[0].boxes:
            x1, y1, x2, y2 = (float(v) for v in caja.xyxy[0].tolist())
            class_id = int(caja.cls.item())
            detecciones.append(
                Detection(
                    bbox=BBox(x1, y1, x2, y2).translated(dx, dy),
                    class_id=class_id,
                    label=self._nombre_de_id[class_id],
                    confidence=float(caja.conf.item()),
                )
            )
        return detecciones
