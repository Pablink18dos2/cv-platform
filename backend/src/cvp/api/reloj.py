"""El reloj de la app, como dependencia de FastAPI.

Es lo que hace que ninguna ruta llame a `datetime.now()`: piden `ahora` y reciben lo que
`crear_app` dejo en `app.state.clock`, que en produccion es la hora del sistema en UTC y
en los tests, lo que el test diga.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Request

__all__ = ["ahora"]


def ahora(request: Request) -> datetime:
    marca: datetime = request.app.state.clock()
    if marca.tzinfo is None or marca.tzinfo.utcoffset(marca) is None:
        raise ValueError("el reloj de la API tiene que devolver un datetime con zona horaria")
    return marca.astimezone(UTC)
