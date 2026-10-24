"""El guardia del encuadre: avisa de que la camara se ha movido (#34).

Sin esto el worker seguiria publicando `spaces_free` y `crossings` sobre poligonos que ya
no estan encima de ninguna plaza, con graficas plausibles y contenido falso.

**Como se mide.** Se compara cada fotograma con una imagen de referencia con puntos SIFT
(Lowe 0.8) y se ajusta una transformacion de semejanza (traslacion, giro y zoom) con
RANSAC. La puntuacion no cuenta puntos: mide **cuanto se desplaza la imagen** segun esa
transformacion, el maximo de las cuatro esquinas y el centro, en fraccion del ancho:

    score = 1 - desplazamiento / 1 %      (0 si hay menos de 10 puntos coherentes)

Es 1 con el mismo encuadre y 0 cuando algun punto se ha movido el 1 % del ancho (19 px en
1920). Se eligio porque lo que cambia con la luz, el clima o los coches es **cuantos**
puntos casan, no **donde**: los que sobreviven (edificios, farolas, el bordillo) siguen
dando el desplazamiento correcto, y los coches y las sombras quedan como outliers de
RANSAC. ORB no sirve aqui (3-8 puntos coherentes entre la referencia de octubre y el
directo de la tarde, contra 20-60 de SIFT) y la correlacion de fase sobre bordes cae a 0,09
con el mismo encuadre y otra luz. Cuesta ~77 ms por fotograma a 640 px de ancho, asi que se
ejecuta cada `every_n_frames` y no en cada uno.

**Medido** (`framing_score`, con la configuracion por defecto), sobre fotogramas reales del
directo del proyecto 01:

    referencia oct-2026 vs directo, 3 tomas con minutos de diferencia     0,82 0,82 0,84
    referencia ago-2026 vs oct-2026 (otra estacion, hojas)                0,90
    referencia ago-2026 vs las 3 tomas del directo (tarde, contraluz)     0,88 0,89 0,88
    referencia ago-2026 (sol) vs fixture lluvioso                         0,94
    oct-2026 vs directo desplazado 5 / 10 / 25 / 60 px (de 1920)          0,63 / 0,37 / 0 / 0
    oct-2026 vs directo girado 1 grado, o con zoom 1,05                   0 / 0

Los movimientos son **sinteticos** (el directo real, deformado): no hay ningun fotograma
real con la camara movida. Los de agosto y octubre, que se tomaban por dos encuadres
distintos, son el mismo con 1-2 px de diferencia: no sirven como ejemplo de camara movida.
Por eso `min_similarity` es 0,6: entre el peor encuadre bueno medido (0,82) y el movimiento
mas pequeno que merece aviso (10 px, 0,37).

**Lo que no se ha podido probar**: la noche (sin luz diurna pueden no salir 10 puntos
coherentes y el score cae a 0 con la camara quieta: es un aviso falso, no un fallo
silencioso) y la nieve. Tampoco una imagen con un rotulo o un logo fijos superpuestos, que
RANSAC daria por buenos aunque la camara se mueva.

**No suprime a los demas analizadores**: solo emite `framing_score`, `framing_lost` y
`framing_restored` y deja que el dashboard avise. Ignora tracks y detecciones. Los tres
nombres son contrato con el dashboard.

**Histeresis en segundos de video** (un camion parado delante no dispara la alarma): el
estado confirmado cambia cuando la puntuacion lleva `lost_after_s` seguidos por debajo del
umbral, o `restored_after_s` seguidos por encima. El estado inicial es "bien": si la camara
ya esta movida al arrancar, el aviso sale tras `lost_after_s`, no se queda mudo.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from cvp.analyzers.drawing import BLANCO, ROJO, copia_para_anotar, dibujar_etiqueta
from cvp.core import AnalyzerOutput, Detection, Event, Frame, Metric, Track
from cvp.core.registry import ANALYZERS

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import FramingGuardAnalyzer as FramingGuardConfig

__all__ = ["FramingGuardAnalyzer"]

# Contrato con el dashboard.
METRICA_SCORE = "framing_score"
EVENTO_PERDIDO = "framing_lost"
EVENTO_RECUPERADO = "framing_restored"

ANCHO_DE_TRABAJO = 640
MIN_PUNTOS = 10
DESPLAZAMIENTO_MAXIMO = 0.01  # fraccion del ancho a la que la puntuacion llega a 0


def _gris(imagen: np.ndarray) -> np.ndarray:
    alto = round(imagen.shape[0] * ANCHO_DE_TRABAJO / imagen.shape[1])
    pequena = cv2.resize(imagen, (ANCHO_DE_TRABAJO, alto), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(pequena, cv2.COLOR_BGR2GRAY)


@ANALYZERS.register("framing_guard")
class FramingGuardAnalyzer:
    """Emite `framing_score` en cada `process()` y `framing_lost` / `framing_restored` al
    cambiar el estado confirmado, con `{"score": ...}`. Ver el modulo."""

    def __init__(
        self,
        reference: np.ndarray,
        *,
        name: str = "framing_guard",
        every_n_frames: int = 1,
        min_similarity: float = 0.6,
        lost_after_s: float = 30.0,
        restored_after_s: float = 30.0,
    ) -> None:
        if not name.strip():
            raise ValueError("name no puede estar vacio: es la columna `analyzer` de cada serie")
        if every_n_frames < 1:
            raise ValueError(f"every_n_frames es una cadencia >= 1, no {every_n_frames!r}")
        if not 0.0 < min_similarity <= 1.0:
            raise ValueError(f"min_similarity esta en (0, 1], no {min_similarity!r}")
        if lost_after_s <= 0 or restored_after_s <= 0:
            raise ValueError("lost_after_s y restored_after_s son segundos > 0")
        self.name = name
        self.every_n_frames = int(every_n_frames)
        self.min_similarity = float(min_similarity)
        self.lost_after_s = float(lost_after_s)
        self.restored_after_s = float(restored_after_s)

        self._sift = cv2.SIFT_create(1500)
        self._matcher = cv2.BFMatcher()
        self._ref_alto, self._ref_ancho = _gris(reference).shape
        self._ref_puntos, self._ref_descriptores = self._sift.detectAndCompute(
            _gris(reference), None
        )
        if self._ref_descriptores is None or len(self._ref_descriptores) < MIN_PUNTOS:
            raise ValueError("la imagen de referencia no tiene textura suficiente para comparar")

        self._bien = True
        self._desde: datetime | None = None  # inicio de la racha contraria al estado

    @classmethod
    def from_config(cls, cfg: FramingGuardConfig, **kwargs: Any) -> FramingGuardAnalyzer:
        referencia = cv2.imread(str(Path(cfg.reference_image)))
        if referencia is None:
            raise ValueError(f"{cfg.reference_image}: no es una imagen que OpenCV pueda leer")
        return cls(
            referencia,
            name=cfg.effective_name,
            every_n_frames=cfg.every_n_frames,
            min_similarity=cfg.min_similarity,
            lost_after_s=cfg.lost_after_s,
            restored_after_s=cfg.restored_after_s,
            **kwargs,
        )

    @property
    def encuadre_correcto(self) -> bool:
        """El estado confirmado: `False` desde `framing_lost` hasta `framing_restored`."""
        return self._bien

    def puntuar(self, imagen: np.ndarray) -> float:
        """La puntuacion 0..1 de una imagen contra la referencia (ver el modulo)."""
        puntos, descriptores = self._sift.detectAndCompute(_gris(imagen), None)
        if descriptores is None or len(descriptores) < 2:
            return 0.0
        pares = self._matcher.knnMatch(self._ref_descriptores, descriptores, k=2)
        buenos = [p[0] for p in pares if len(p) == 2 and p[0].distance < 0.8 * p[1].distance]
        if len(buenos) < MIN_PUNTOS:
            return 0.0
        origen = np.float32([self._ref_puntos[m.queryIdx].pt for m in buenos])
        destino = np.float32([puntos[m.trainIdx].pt for m in buenos])
        matriz, coherentes = cv2.estimateAffinePartial2D(
            origen,
            destino,
            method=cv2.RANSAC,
            ransacReprojThreshold=0.005 * self._ref_ancho,
        )
        if matriz is None or int(coherentes.sum()) < MIN_PUNTOS:
            return 0.0
        ancho, alto = self._ref_ancho, self._ref_alto
        referencias = np.float32(
            [[0, 0], [ancho, 0], [ancho, alto], [0, alto], [ancho / 2, alto / 2]]
        )
        movidas = referencias @ matriz[:, :2].T + matriz[:, 2]
        desplazamiento = float(np.hypot(*(movidas - referencias).T).max()) / ancho
        return max(0.0, 1.0 - desplazamiento / DESPLAZAMIENTO_MAXIMO)

    def process(
        self, frame: Frame, tracks: list[Track], detections: list[Detection]
    ) -> AnalyzerOutput:
        del tracks, detections
        score = self.puntuar(frame.image)
        t = frame.timestamp
        eventos: list[Event] = []
        if (score >= self.min_similarity) == self._bien:
            self._desde = None
        else:
            self._desde = self._desde or t
            espera = self.lost_after_s if self._bien else self.restored_after_s
            if (t - self._desde).total_seconds() >= espera:
                self._bien, self._desde = not self._bien, None
                eventos.append(
                    Event(
                        analyzer=self.name,
                        name=EVENTO_RECUPERADO if self._bien else EVENTO_PERDIDO,
                        timestamp=t,
                        attributes={"score": score},
                    )
                )
        metrica = Metric(analyzer=self.name, name=METRICA_SCORE, value=score, timestamp=t)
        return AnalyzerOutput(events=eventos, metrics=[metrica])

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray:
        """Una copia, con un aviso rojo mientras el encuadre esta perdido."""
        del tracks
        lienzo = copia_para_anotar(image)
        if not self._bien:
            dibujar_etiqueta(
                lienzo, "ENCUADRE PERDIDO", (12, 12), color_fondo=ROJO, color_texto=BLANCO
            )
        return lienzo
