"""Los errores, con una sola forma: `{"error": {"code": "...", "message": "..."}}`.

Es la forma que fija `_docs/api.md`, y la tienen **todos** los errores que salen de la
API, tambien los que FastAPI produce solo: un 404 por ruta inexistente, un 405, y los 422
de validacion, que ademas dicen que campo fallo en `details`. Un cliente generado desde
`openapi.yaml` tiene asi un unico tipo de error que entender.

Los codigos que existen:

- `project_not_found` (404): el `{id}` no esta en la tabla `project`, o ni siquiera tiene
  forma de id de proyecto.
- `snapshot_not_found` (404): el proyecto no tiene todavia un fotograma en disco.
- `invalid_range` (400): `from` > `to`, o una ventana de mas de 7 dias.
- `validation_error` (422): un parametro que no pasa la validacion, con `details[].field`.
- `not_found`, `method_not_allowed`, `http_error`: los de la propia infraestructura HTTP.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "ApiError",
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "RESPUESTAS_DE_ERROR",
    "registrar_manejadores",
]


class ErrorDetail(BaseModel):
    field: str = Field(description="El parametro que no pasa la validacion, p. ej. `from`.")
    message: str


class ErrorBody(BaseModel):
    code: str = Field(description="Un identificador estable, para que el cliente decida.")
    message: str = Field(description="Para una persona. No es estable: no se compara.")
    details: list[ErrorDetail] | None = Field(
        default=None, description="Solo en `validation_error`: un elemento por campo."
    )


class ErrorResponse(BaseModel):
    """La unica forma de error de la API."""

    error: ErrorBody


class ApiError(Exception):
    """Un error con codigo de estado y codigo de error. Lo convierte el manejador."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: list[ErrorDetail] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def cuerpo(self) -> dict[str, Any]:
        return ErrorResponse(
            error=ErrorBody(code=self.code, message=self.message, details=self.details)
        ).model_dump(exclude_none=True)


# Lo que cada ruta declara en `openapi.yaml` para sus errores. El 422 sustituye al
# `HTTPValidationError` que FastAPI pondria por defecto: el cliente ve la misma forma.
RESPUESTAS_DE_ERROR: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse, "description": "Un parametro no pasa la validacion."},
}

_CODIGOS_HTTP = {404: "not_found", 405: "method_not_allowed"}


def _campo_de(loc: tuple[Any, ...]) -> str:
    """`("query", "limit")` -> `limit`; `("path", "id")` -> `id`."""
    partes = [str(p) for p in loc if p not in ("query", "path", "body", "header")]
    return ".".join(partes) if partes else "request"


def registrar_manejadores(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.cuerpo())

    @app.exception_handler(RequestValidationError)
    async def _validacion(_request: Request, exc: RequestValidationError) -> JSONResponse:
        detalles = [
            ErrorDetail(field=_campo_de(tuple(e.get("loc", ()))), message=str(e.get("msg", "")))
            for e in exc.errors()
        ]
        campos = ", ".join(sorted({d.field for d in detalles}))
        error = ApiError(
            422, "validation_error", f"parametros invalidos: {campos}", details=detalles
        )
        return JSONResponse(status_code=422, content=error.cuerpo())

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codigo = _CODIGOS_HTTP.get(exc.status_code, "http_error")
        error = ApiError(exc.status_code, codigo, str(exc.detail))
        return JSONResponse(
            status_code=exc.status_code, content=error.cuerpo(), headers=exc.headers
        )
