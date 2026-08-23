"""Errores de los connectors de video."""

from __future__ import annotations


class SourceError(RuntimeError):
    """No se ha podido leer de una fuente de video.

    Envuelve todo lo que puede salir mal en un adaptador: la ruta no existe, el fichero no
    es un video, OpenCV no sabe el frame rate, el directo se ha cortado. Ninguna excepcion
    de OpenCV escapa hacia arriba: OpenCV avisa de casi todo devolviendo `False` en vez de
    lanzando, asi que quien llama no puede protegerse con un `except` si no le damos uno.
    """
