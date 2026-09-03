"""La forma canonica en JSON de `Event` y `Metric`.

Un solo sitio convierte, y lo usan por igual el sink `jsonl`, la tabla de SQLite y las
respuestas de la API. Sin esto los tres inventarian formas distintas del mismo dato y el
dashboard acabaria traduciendo entre ellas.

Vive en `cvp.core` a proposito: el core **ya** conoce la serializabilidad JSON, porque
`Event` rechaza en su constructor lo que no se puede serializar y `Metric` exige `labels` de
`str` a `str`. O sea, ya validaba contra un contrato que no estaba escrito en ningun sitio.
Escribirlo fuera dejaria la validacion en un modulo y la forma en otro, libres de separarse.

Solo biblioteca estandar: `json` y `datetime`. El aislamiento del core se mantiene entero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cvp.core.types import Event, Metric

__all__ = [
    "event_from_dict",
    "event_to_dict",
    "metric_from_dict",
    "metric_to_dict",
]

_CAMPOS_EVENT = ("analyzer", "name", "timestamp", "attributes")
_CAMPOS_METRIC = ("analyzer", "name", "value", "timestamp", "labels")


def _marca_a_texto(marca: datetime) -> str:
    """ISO-8601 en UTC con sufijo `Z`.

    `isoformat()` escribe `+00:00`, que es igual de valido pero mas largo y menos habitual en
    APIs. Se normaliza a `Z` al escribir; al leer se aceptan las dos.
    """
    return marca.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _texto_a_marca(valor: Any, campo: str) -> datetime:
    if not isinstance(valor, str):
        raise ValueError(f"{campo} debe ser una cadena ISO-8601, no {type(valor).__name__}")
    try:
        # `fromisoformat` de 3.11+ entiende la `Z`, pero se normaliza igualmente para no
        # depender de esa version en el futuro.
        marca = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{campo} no es una fecha ISO-8601 valida: {valor!r}") from exc
    if marca.tzinfo is None:
        # El core exige zona, y la deserializacion no puede ser la puerta trasera que lo salte.
        raise ValueError(f"{campo} no lleva zona horaria: {valor!r}")
    return marca


def _comprobar_campos(datos: dict[str, Any], esperados: tuple[str, ...], tipo: str) -> None:
    recibidos = set(datos)
    faltan = sorted(set(esperados) - recibidos)
    sobran = sorted(recibidos - set(esperados))
    if faltan:
        raise ValueError(f"a un {tipo} le faltan campos: {faltan}")
    if sobran:
        # Un campo de mas casi siempre es una errata o un esquema de otra version, y
        # tragarselo pierde datos en silencio.
        raise ValueError(f"un {tipo} trae campos que no existen: {sobran}")


def event_to_dict(evento: Event) -> dict[str, Any]:
    return {
        "analyzer": evento.analyzer,
        "name": evento.name,
        "timestamp": _marca_a_texto(evento.timestamp),
        "attributes": dict(evento.attributes),
    }


def event_from_dict(datos: dict[str, Any]) -> Event:
    _comprobar_campos(datos, _CAMPOS_EVENT, "Event")
    return Event(
        analyzer=datos["analyzer"],
        name=datos["name"],
        timestamp=_texto_a_marca(datos["timestamp"], "timestamp"),
        attributes=datos["attributes"],
    )


def metric_to_dict(metrica: Metric) -> dict[str, Any]:
    return {
        "analyzer": metrica.analyzer,
        "name": metrica.name,
        "value": metrica.value,
        "timestamp": _marca_a_texto(metrica.timestamp),
        "labels": dict(metrica.labels),
    }


def metric_from_dict(datos: dict[str, Any]) -> Metric:
    _comprobar_campos(datos, _CAMPOS_METRIC, "Metric")
    return Metric(
        analyzer=datos["analyzer"],
        name=datos["name"],
        value=datos["value"],
        timestamp=_texto_a_marca(datos["timestamp"], "timestamp"),
        labels=datos["labels"],
    )
