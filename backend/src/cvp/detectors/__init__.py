"""Detectores de objetos.

Importar este paquete registra los detectores en `cvp.core.registry.DETECTORS`, que es lo
que permite que un `type: yolo` del YAML se resuelva a una clase.
"""

from __future__ import annotations

from cvp.detectors.errors import DetectorError
from cvp.detectors.yolo import YoloDetector

__all__ = ["DetectorError", "YoloDetector"]
