"""Crear el engine y el esquema, y poner SQLite en las condiciones que este sistema exige.

Todo lo especifico de un dialecto vive aqui y no en `models.py`: el esquema es el mismo
para SQLite y para Postgres, y lo que cambia es como se abre la conexion.

Lo que se le pide a SQLite, y por que:

- **WAL** (`journal_mode=WAL`). Es la condicion para que el worker escriba mientras la API
  lee. Con el modo por defecto (`delete`), un lector bloquea al escritor y viceversa: la
  API haria esperar al worker en cada peticion del dashboard, y el bucle de inferencia
  perderia fotogramas por una consulta. WAL es una propiedad **del fichero**, no de la
  conexion: se pone una vez y sobrevive a cerrar el proceso.
- **`synchronous=NORMAL`.** Con WAL es el ajuste recomendado y la unica perdida posible es
  la de las ultimas transacciones confirmadas ante un **corte de corriente**; un fallo del
  proceso no pierde nada ya confirmado. A cambio no se paga un `fsync` por transaccion,
  que a 5 fps y con lotes de escritura es la diferencia entre notarlo y no notarlo.
- **`foreign_keys=ON`.** SQLite las ignora por defecto, asi que sin esto una fila de
  `event` podria apuntar a un `run` que no existe y el esquema seria decorativo. En
  Postgres ya estan activas y no hay nada que encender.

Con Postgres no se toca nada: la URL manda, y `crear_engine` no le pone `PRAGMA` a una
base que no los entiende.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import make_url
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

# Importar los modelos es lo que llena `SQLModel.metadata`: sin esto `crear_esquema` no
# crearia ninguna tabla, y lo haria en silencio.
from cvp.storage import models as _modelos

__all__ = ["URL_POR_DEFECTO", "crear_engine", "crear_esquema", "journal_mode"]

_log = logging.getLogger(__name__)

# La misma que el valor por defecto de `sinks: [{type: sqlite}]` en `cvp.pipeline.config`.
URL_POR_DEFECTO = "sqlite:///data/cvp.sqlite3"

_ = _modelos  # el import es por su efecto: registrar las tablas en los metadatos.


def crear_engine(url: str = URL_POR_DEFECTO, *, echo: bool = False) -> Engine:
    """El engine de `url`, con SQLite ya configurado y su directorio creado.

    Crear el directorio es del sink, no de la configuracion (decision de #3), y este es el
    sitio donde el sink lo hace: un `sqlite:///data/cvp.sqlite3` recien clonado no tiene
    todavia un `data/`, y fallar por eso al arrancar seria un tropiezo tonto.
    """
    direccion = make_url(url)
    es_sqlite = direccion.get_backend_name() == "sqlite"
    if es_sqlite and direccion.database and direccion.database != ":memory:":
        Path(direccion.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    motor = create_engine(url, echo=echo)
    if es_sqlite:
        _configurar_sqlite(motor)
    return motor


def _configurar_sqlite(motor: Engine) -> None:
    """Aplica los `PRAGMA` en **cada** conexion nueva.

    Va en un `listener` y no en un `execute` suelto porque el pool abre conexiones cuando
    le hace falta: los `PRAGMA` de sesion (`foreign_keys`, `synchronous`) valen solo para
    la conexion que los ejecuto, y una segunda conexion sin ellos volveria a ignorar las
    claves ajenas sin decir nada.
    """

    @sa_event.listens_for(motor, "connect")
    def _pragmas(conexion: Any, _registro: Any) -> None:
        cursor = conexion.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def crear_esquema(motor: Engine) -> None:
    """Crea las tablas que falten. Idempotente: arrancar dos veces no rompe nada.

    No hay migraciones a proposito (esta fuera del alcance de #9): mientras no haya datos
    que conservar, el esquema se recrea borrando el fichero. El dia que los haya, aqui es
    donde entra Alembic.
    """
    SQLModel.metadata.create_all(motor)


def journal_mode(motor: Engine) -> str:
    """El `journal_mode` que declara la base, en minusculas. `''` si no es SQLite.

    Existe para poder comprobarlo: "esta en WAL" no es una intencion escrita en un
    comentario, es lo que responde la base.
    """
    if motor.dialect.name != "sqlite":
        return ""
    with motor.connect() as conexion:
        modo = conexion.exec_driver_sql("PRAGMA journal_mode").scalar()
    return str(modo).lower()
