"""La ventana de una consulta de metricas: `from`, `to`, y lo que no se acepta.

- **Sin `from` ni `to`, la ultima hora** hasta ahora, y "ahora" lo da el reloj inyectado.
  Con solo `to`, la hora anterior a `to`; con solo `from`, de `from` hasta ahora.
- **La rejilla va por minutos enteros, y cierra por los dos lados.** `from` y `to` se
  truncan al minuto UTC (el mismo `minuto_de` que el agregador) y los dos minutos entran:
  pedir `from=14:20:30&to=14:24:10` da los cinco minutos de 14:20 a 14:24.
- **Como mucho 7 dias.** Es lo que cabe en una peticion sin que una serie por minuto
  sobre una base de meses tumbe la API: 10.081 puntos por serie. Mas, 400 `invalid_range`.
- `from` > `to` es tambien 400 `invalid_range`, con el motivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cvp.api.errors import ApiError
from cvp.storage import minuto_de

__all__ = ["RANGO_MAXIMO", "VENTANA_POR_DEFECTO", "Ventana", "iso", "ventana_de"]

VENTANA_POR_DEFECTO = timedelta(hours=1)
RANGO_MAXIMO = timedelta(days=7)
_UN_MINUTO = timedelta(minutes=1)


def iso(marca: datetime) -> str:
    """ISO-8601 en UTC con `Z`, como la forma canonica de #19. Para los mensajes."""
    return marca.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Ventana:
    """`[desde, hasta]`, los dos truncados al minuto UTC y los dos incluidos."""

    desde: datetime
    hasta: datetime

    @property
    def minutos(self) -> list[datetime]:
        """La rejilla: un minuto por punto, de `desde` a `hasta` incluidos."""
        total = int((self.hasta - self.desde) / _UN_MINUTO) + 1
        return [self.desde + i * _UN_MINUTO for i in range(total)]


def ventana_de(
    desde: datetime | None,
    hasta: datetime | None,
    *,
    ahora: datetime,
    por_defecto: timedelta = VENTANA_POR_DEFECTO,
    maximo: timedelta = RANGO_MAXIMO,
) -> Ventana:
    if hasta is None:
        hasta = ahora
    if desde is None:
        desde = hasta - por_defecto
    desde = minuto_de(desde.astimezone(UTC))
    hasta = minuto_de(hasta.astimezone(UTC))
    if desde > hasta:
        raise ApiError(
            400,
            "invalid_range",
            f"from ({iso(desde)}) es posterior a to ({iso(hasta)})",
        )
    if hasta - desde > maximo:
        dias = maximo.total_seconds() / 86400
        raise ApiError(
            400,
            "invalid_range",
            f"la ventana de {iso(desde)} a {iso(hasta)} supera el maximo de "
            f"{dias:g} dias por peticion; pide varias ventanas mas cortas",
        )
    return Ventana(desde=desde, hasta=hasta)
