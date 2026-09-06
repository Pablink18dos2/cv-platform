"""El formato canonico del `run_id`, decidido en #9.

#8 lo dejo como cadena opaca a proposito: mientras nadie lo guardara, lo unico que hacia
falta era que fuese estable durante la ejecucion y distinto en dos arranques. Desde que es
la clave primaria de una tabla y la que ordena las ejecuciones en la API, el formato deja
de ser un detalle del runner y pasa a ser un contrato.

    20260824T142018Z-2808cc47
    |--------------| |------|
      instante UTC    8 hex

Tres propiedades, y cada una responde a una pregunta que se hace de verdad:

- **Ordenable lexicograficamente por tiempo.** `ORDER BY id DESC` da las ejecuciones mas
  recientes sin una columna extra ni un indice mas, y `LIKE '20260824%'` es "las de ese
  dia". El instante va con ancho fijo y en UTC precisamente para eso: con `+02:00` o con
  microsegundos opcionales el orden alfabetico dejaria de ser el orden cronologico.
- **Unico aunque dos arranques caigan en el mismo segundo.** Es lo que hace el sufijo
  aleatorio, y por eso no se sustituye por un contador: dos workers del mismo proyecto no
  se coordinan entre si.
- **Legible.** Se lee en un log, se copia a mano en una consulta y se reconoce sin
  traducirlo.

Este modulo es de biblioteca estandar y no importa SQLModel: lo usa el runner, que genera
el identificador, y lo usa `cvp.storage`, que lo guarda.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

__all__ = [
    "FORMATO_RUN_ID",
    "RUN_ID_RE",
    "instante_de_run_id",
    "nuevo_run_id",
    "validar_run_id",
]

# El `strftime` del instante y la expresion que lo reconoce, juntos para que no se
# separen: el que genera y el que valida tienen que hablar del mismo formato.
FORMATO_RUN_ID = "%Y%m%dT%H%M%SZ"
RUN_ID_RE = re.compile(r"^(?P<instante>\d{8}T\d{6}Z)-(?P<sufijo>[0-9a-f]{8})$")


def nuevo_run_id(ahora: datetime) -> str:
    """El identificador de una ejecucion que empieza en `ahora`.

    El reloj se pasa como argumento, no se consulta aqui: es la misma regla que en el resto
    del sistema, y es lo que permite que un test fije el `run_id` entero.
    """
    if ahora.tzinfo is None or ahora.tzinfo.utcoffset(ahora) is None:
        raise ValueError(
            "el instante de un run_id necesita zona horaria: sin ella no identifica un instante"
        )
    marca = ahora.astimezone(UTC).strftime(FORMATO_RUN_ID)
    return f"{marca}-{uuid.uuid4().hex[:8]}"


def validar_run_id(run_id: str) -> str:
    """Devuelve `run_id` si encaja en el formato canonico, o lanza `ValueError`.

    Se comprueba al escribir en la base de datos y no solo al generar: la fila de `run` es
    la que ordena la API, y una clave con otra forma se colaria sin romper nada hasta que
    alguien mirase el orden.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ValueError(
            f"run_id invalido: {run_id!r}. El formato canonico es AAAAMMDDTHHMMSSZ-xxxxxxxx, "
            "por ejemplo 20260824T142018Z-2808cc47"
        )
    # Que encaje en la expresion no basta: `20260899T...` la pasa y no es una fecha.
    instante_de_run_id(run_id)
    return run_id


def instante_de_run_id(run_id: str) -> datetime:
    """El instante UTC en que arranco la ejecucion, sacado del propio identificador.

    Es lo que permite rellenar `run.started_at` sin consultar el reloj en el sink: el dato
    ya viaja dentro del `run_id`.
    """
    encaje = RUN_ID_RE.match(run_id or "")
    if encaje is None:
        raise ValueError(
            f"run_id invalido: {run_id!r}. El formato canonico es AAAAMMDDTHHMMSSZ-xxxxxxxx"
        )
    try:
        return datetime.strptime(encaje["instante"], FORMATO_RUN_ID).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"run_id invalido: {run_id!r} no lleva una fecha real: {exc}") from exc
