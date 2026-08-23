"""Connectors de video.

Importar este paquete registra todas las fuentes en `cvp.core.registry.SOURCES`, que es
lo que permite que un `type: file` del YAML se resuelva a una clase.
"""

from __future__ import annotations

from cvp.sources.errors import SourceError
from cvp.sources.file import FileVideoSource

__all__ = ["FileVideoSource", "SourceError"]
