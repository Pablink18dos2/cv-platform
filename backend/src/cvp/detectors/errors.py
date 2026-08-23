"""Errores de los detectores."""

from __future__ import annotations


class DetectorError(RuntimeError):
    """No se ha podido cargar o usar un modelo de deteccion.

    Envuelve lo que ultralytics y torch lanzan por su cuenta, que suele venir con un rastro
    largo y poco util. Quien llama solo necesita saber que se buscó, donde, y que no estaba.
    """
