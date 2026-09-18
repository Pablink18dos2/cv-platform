"""La conexion de lectura: el engine de la app y la sesion por peticion.

La API abre la base por `DATABASE_URL`, la misma variable que el worker (#9), con el
mismo `crear_engine` de `cvp.storage`: para SQLite eso significa WAL, que es justo lo que
permite que el worker escriba mientras la API lee sin que ninguno bloquee al otro.

**La API no escribe filas.** Lo unico que crea es el esquema si no existe —el mismo
`create_all` idempotente que usa el sink— y es lo que hace que una base ausente o vacia
responda con listas vacias en vez de con un 500 por "no such table". Arrancar la API
contra una base que aun no existe deja un fichero con cinco tablas vacias, que es
exactamente lo que el worker se encontraria al arrancar el.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlmodel import Session

from cvp.storage import crear_engine, crear_esquema

__all__ = ["abrir_engine", "sesion"]


def abrir_engine(url: str) -> Engine:
    motor = crear_engine(url)
    crear_esquema(motor)
    return motor


def sesion(request: Request) -> Iterator[Session]:
    """Una sesion por peticion, sobre el engine que `crear_app` dejo en `app.state`."""
    with Session(request.app.state.engine) as session:
        yield session
