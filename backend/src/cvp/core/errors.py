"""Errores del registro por nombre.

Viven aparte de `registry.py` porque quien los captura (la fabrica que lee el YAML, el
CLI) no tiene por que importar el registro entero. Las dos son subclases de `ValueError`:
un `type:` que no existe o que esta duplicado es un valor de configuracion invalido, y
quien ya capture `ValueError` al validar la configuracion no necesita cambiar nada.
"""

from __future__ import annotations

__all__ = ["DuplicateRegistrationError", "UnknownTypeError"]


class DuplicateRegistrationError(ValueError):
    """Dos clases distintas intentan registrarse con el mismo nombre.

    Salta al importar el modulo que registra, no al resolver el nombre: asi el conflicto
    aparece en el arranque y no en el minuto 40.
    """


class UnknownTypeError(ValueError):
    """Se pide un nombre que nadie ha registrado.

    Es el error que ve quien escribe `type: yolo11` en el YAML, asi que el mensaje lleva
    la lista ordenada de nombres disponibles para ese bloque.
    """
