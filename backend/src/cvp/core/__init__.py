"""El contrato del sistema: los tipos que viajan entre bloques, los cinco `Protocol` y
el registro por nombre que resuelve el `type:` del YAML.

`cvp.core` **no importa nada del resto de `cvp`**, ni torch, ni OpenCV, ni ultralytics:
solo biblioteca estandar y numpy. Es lo que mantiene la suite de tests rapida y lo que
permite que la API, que solo lee de la base de datos, no cargue un modelo por accidente.
`tests/unit/test_core_aislamiento.py` lo comprueba en un subproceso.
"""

from __future__ import annotations

from cvp.core.errors import DuplicateRegistrationError, UnknownTypeError
from cvp.core.protocols import Analyzer, Detector, Sink, Tracker, VideoSource
from cvp.core.registry import (
    ANALYZERS,
    DETECTORS,
    SINKS,
    SOURCES,
    TRACKERS,
    register_analyzer,
    register_detector,
    register_sink,
    register_source,
    register_tracker,
)
from cvp.core.serialization import (
    event_from_dict,
    event_to_dict,
    metric_from_dict,
    metric_to_dict,
)
from cvp.core.types import (
    AnalyzerOutput,
    BBox,
    Detection,
    Event,
    Frame,
    Metric,
    SourceInfo,
    Track,
)

__all__ = [
    "ANALYZERS",
    "DETECTORS",
    "SINKS",
    "SOURCES",
    "TRACKERS",
    "Analyzer",
    "AnalyzerOutput",
    "BBox",
    "Detection",
    "Detector",
    "DuplicateRegistrationError",
    "Event",
    "Frame",
    "Metric",
    "Sink",
    "SourceInfo",
    "Track",
    "Tracker",
    "UnknownTypeError",
    "VideoSource",
    "event_from_dict",
    "event_to_dict",
    "metric_from_dict",
    "metric_to_dict",
    "register_analyzer",
    "register_detector",
    "register_sink",
    "register_source",
    "register_tracker",
]
