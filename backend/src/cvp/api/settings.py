"""La configuracion de la API: de donde lee y en que puerto escucha.

Sale del entorno, con los mismos nombres y valores por defecto que `.env.example`:
`DATABASE_URL` es **la misma variable que el worker** (#9), para que los dos procesos
miren la misma base sin ponerse de acuerdo; `CVP_API_HOST`, `CVP_API_PORT` y
`CVP_CORS_ORIGINS` son de la API. Lo que no tiene variable de entorno (el directorio del
preview, el intervalo del MJPEG) se cambia construyendo `Settings` a mano, que es lo que
hacen los tests.

Es un dataclass congelado y no un modelo de Pydantic a proposito: no hay nada que validar
que no sea "el puerto es un entero", y `crear_app(Settings(...))` tiene que poder
escribirse en un test sin pasar por el entorno.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cvp.api.preview import DIRECTORIO_PREVIEW
from cvp.storage import URL_POR_DEFECTO

__all__ = [
    "HOST_POR_DEFECTO",
    "INTERVALO_STREAM_POR_DEFECTO_S",
    "ORIGENES_POR_DEFECTO",
    "PUERTO_POR_DEFECTO",
    "Settings",
]

HOST_POR_DEFECTO = "127.0.0.1"
PUERTO_POR_DEFECTO = 8000
# El dashboard de Vite en desarrollo, que es el unico origen distinto del de la API.
ORIGENES_POR_DEFECTO = ("http://localhost:5173",)
# Cada cuanto el MJPEG mira si el fichero ha cambiado. 250 ms son 4 comprobaciones por
# segundo, mas que el ~0,5-5 fps al que el worker escribe el preview.
INTERVALO_STREAM_POR_DEFECTO_S = 0.25


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    database_url: str = URL_POR_DEFECTO
    host: str = HOST_POR_DEFECTO
    port: int = PUERTO_POR_DEFECTO
    cors_origins: tuple[str, ...] = ORIGENES_POR_DEFECTO
    # Donde el sink `preview` deja `<id>.jpg`. Relativo a donde se lanza el proceso, igual
    # que la base de datos por defecto y por el mismo motivo.
    preview_dir: Path = field(default_factory=lambda: DIRECTORIO_PREVIEW)
    stream_interval_s: float = INTERVALO_STREAM_POR_DEFECTO_S

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port es un puerto TCP, de 1 a 65535, no {self.port!r}")
        if self.stream_interval_s <= 0:
            raise ValueError(f"stream_interval_s son segundos > 0, no {self.stream_interval_s!r}")
        object.__setattr__(self, "preview_dir", Path(self.preview_dir))

    @classmethod
    def desde_entorno(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Lee las variables de `.env.example`. Una variable vacia cuenta como ausente.

        Es la misma regla que la configuracion del worker (#3): una cadena vacia colada
        en una URL o en un puerto produce un fallo lejos de su causa.
        """
        entorno = os.environ if env is None else env

        def valor(nombre: str) -> str | None:
            crudo = entorno.get(nombre)
            return crudo.strip() if crudo and crudo.strip() else None

        puerto_crudo = valor("CVP_API_PORT")
        try:
            puerto = int(puerto_crudo) if puerto_crudo is not None else PUERTO_POR_DEFECTO
        except ValueError:
            raise ValueError(f"CVP_API_PORT={puerto_crudo!r} no es un puerto") from None
        origenes_crudos = valor("CVP_CORS_ORIGINS")
        origenes = (
            tuple(o.strip() for o in origenes_crudos.split(",") if o.strip())
            if origenes_crudos is not None
            else ORIGENES_POR_DEFECTO
        )
        return cls(
            database_url=valor("DATABASE_URL") or URL_POR_DEFECTO,
            host=valor("CVP_API_HOST") or HOST_POR_DEFECTO,
            port=puerto,
            cors_origins=origenes,
        )
