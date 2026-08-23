"""Tracker ByteTrack sobre el paquete `trackers` de Roboflow.

Se eligio `trackers` y no las otras dos opciones evidentes, y conviene que quede escrito:

- `sv.ByteTrack` de supervision esta **deprecado desde la 0.28 y se elimina en la 0.31**.
- `BYTETracker` de ultralytics no acepta un adaptador sencillo (su `update` exige un objeto
  indexable tipo `Boxes` y su contrato interno acaba de cambiar), y ademas importarlo hace
  que ultralytics **instale `lap` en tiempo de ejecucion**: un `pip install` a mitad de una
  ejecucion de worker no es aceptable.

Que esto sea reversible es el motivo de que `Tracker` sea un `Protocol`: si `trackers` no
envejece bien se cambia este fichero y nadie mas se entera.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import supervision as sv

from cvp.core import BBox, Detection, Frame, Track
from cvp.core.registry import TRACKERS

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import ByteTrackTracker as ByteTrackConfig

_SIN_CONFIRMAR = -1


@TRACKERS.register("bytetrack")
class ByteTrackTracker:
    """Da identidad estable a las detecciones entre fotogramas.

    **Devuelve menos tracks que detecciones en los primeros fotogramas de la vida de un
    vehiculo, y es deliberado.** ByteTrack no confirma una identidad hasta que ha visto el
    objeto `minimum_consecutive_frames` veces seguidas; hasta entonces lo marca con id -1 y
    aqui se descarta. Es lo que evita crear una identidad nueva por cada parpadeo del
    detector, que para contar trafico seria mucho peor que perderse los dos primeros
    fotogramas de cada coche.

    `frame_rate` es la tasa a la que **este tracker** ve fotogramas, o sea el `target_fps`
    del proyecto, no los fps nativos del video. Si se le miente, el filtro de Kalman predice
    posiciones para un tiempo que no ha pasado.
    """

    def __init__(
        self,
        *,
        frame_rate: float,
        lost_track_buffer: int = 30,
        track_activation_threshold: float = 0.7,
        minimum_consecutive_frames: int = 2,
        minimum_iou_threshold: float = 0.1,
        tracker: Any | None = None,
    ) -> None:
        if frame_rate <= 0:
            raise ValueError(f"frame_rate debe ser > 0, no {frame_rate!r}")
        self.frame_rate = float(frame_rate)
        self.lost_track_buffer = lost_track_buffer
        self.track_activation_threshold = track_activation_threshold
        self.minimum_consecutive_frames = minimum_consecutive_frames
        self.minimum_iou_threshold = minimum_iou_threshold
        self._inyectado = tracker
        self._tracker = tracker if tracker is not None else self._construir()

    def _construir(self) -> Any:
        from trackers import ByteTrackTracker as ByteTrackImpl

        return ByteTrackImpl(
            frame_rate=self.frame_rate,
            lost_track_buffer=self.lost_track_buffer,
            track_activation_threshold=self.track_activation_threshold,
            minimum_consecutive_frames=self.minimum_consecutive_frames,
            minimum_iou_threshold=self.minimum_iou_threshold,
        )

    @classmethod
    def from_config(
        cls, cfg: ByteTrackConfig, *, frame_rate: float, **kwargs: Any
    ) -> ByteTrackTracker:
        """Construye desde la configuracion de #3.

        `frame_rate` va aparte y es obligatorio: no se escribe en el YAML porque no es una
        decision del bloque `tracker`, es el `target_fps` de la fuente. Lo pasa el runner
        de #8, que es quien conoce los dos.
        """
        return cls(
            frame_rate=frame_rate,
            lost_track_buffer=cfg.lost_track_buffer,
            track_activation_threshold=cfg.track_activation_threshold,
            minimum_consecutive_frames=cfg.minimum_consecutive_frames,
            minimum_iou_threshold=cfg.minimum_iou_threshold,
            **kwargs,
        )

    def update(self, frame: Frame, detections: list[Detection]) -> list[Track]:
        if not detections:
            # Aun sin detecciones hay que avisar al tracker: es como envejecen los tracks
            # perdidos y como acaban expirando en vez de quedarse vivos para siempre.
            self._tracker.update(_vacias())
            return []

        seguidas = self._tracker.update(_a_supervision(detections))
        return _a_tracks(seguidas, detections)

    def reset(self) -> None:
        self._tracker = self._inyectado if self._inyectado is not None else self._construir()


def _vacias() -> sv.Detections:
    return sv.Detections(
        xyxy=np.empty((0, 4), dtype=float),
        confidence=np.empty(0, dtype=float),
        class_id=np.empty(0, dtype=int),
    )


def _a_supervision(detections: list[Detection]) -> sv.Detections:
    return sv.Detections(
        xyxy=np.array(
            [[d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2] for d in detections], dtype=float
        ),
        confidence=np.array([d.confidence for d in detections], dtype=float),
        class_id=np.array([d.class_id for d in detections], dtype=int),
    )


def _a_tracks(seguidas: sv.Detections, originales: list[Detection]) -> list[Track]:
    """Vuelve al mundo del core, descartando lo que el tracker no ha confirmado.

    Las etiquetas se recuperan de las detecciones originales por `class_id`: `sv.Detections`
    no lleva el nombre legible, y reconstruirlo desde el modelo aqui acoplaria el tracker al
    detector.
    """
    if seguidas.tracker_id is None:
        return []
    etiqueta_de_clase = {d.class_id: d.label for d in originales}

    tracks: list[Track] = []
    for i, track_id in enumerate(seguidas.tracker_id):
        if int(track_id) == _SIN_CONFIRMAR:
            continue
        x1, y1, x2, y2 = (float(v) for v in seguidas.xyxy[i])
        class_id = int(seguidas.class_id[i]) if seguidas.class_id is not None else 0
        confidence = float(seguidas.confidence[i]) if seguidas.confidence is not None else 0.0
        tracks.append(
            Track(
                bbox=BBox(x1, y1, x2, y2),
                class_id=class_id,
                label=etiqueta_de_clase.get(class_id, str(class_id)),
                confidence=confidence,
                track_id=int(track_id),
            )
        )
    return tracks
