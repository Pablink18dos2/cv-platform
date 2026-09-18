"""Las fixtures de la API: `base`, `cliente` y `crear_cliente`. El soporte esta en
`api_soporte.py`, que es de donde los tests importan constantes y constructores."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_soporte import Base
from fastapi.testclient import TestClient

from cvp.api import crear_app


@pytest.fixture
def base(tmp_path: Path) -> Iterator[Base]:
    almacen = Base(tmp_path)
    try:
        yield almacen
    finally:
        almacen.cerrar()


@pytest.fixture
def cliente(base: Base) -> Iterator[TestClient]:
    """Un `TestClient` sobre la app construida contra `base`, con su reloj.

    La app lee la base en cada peticion, asi que da igual que el test la pueble antes o
    despues de pedir el cliente: es lo mismo que le pasa a la API con el worker.
    """
    with TestClient(crear_app(base.settings(), clock=base.reloj)) as cliente:
        yield cliente


@pytest.fixture
def crear_cliente(base: Base) -> Iterator[Any]:
    """Para los tests que necesitan otros `Settings` (el intervalo del MJPEG)."""
    abiertos: list[TestClient] = []

    def _crear(**cambios: Any) -> TestClient:
        cliente = TestClient(crear_app(base.settings(**cambios), clock=base.reloj))
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente

    try:
        yield _crear
    finally:
        for cliente in abiertos:
            cliente.__exit__(None, None, None)
