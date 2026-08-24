"""Connectors de video.

Importar este paquete registra todas las fuentes en `cvp.core.registry.SOURCES`, que es
lo que permite que un `type: file` del YAML se resuelva a una clase.
"""

from __future__ import annotations

from cvp.sources.errors import SourceError
from cvp.sources.file import FileVideoSource
from cvp.sources.live import LiveVideoSource, RecentFrameBuffer, VideoTimeline
from cvp.sources.youtube_live import YouTubeLiveSource

__all__ = [
    "FileVideoSource",
    "LiveVideoSource",
    "RecentFrameBuffer",
    "SourceError",
    "VideoTimeline",
    "YouTubeLiveSource",
]
