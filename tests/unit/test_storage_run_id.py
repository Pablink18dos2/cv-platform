"""El formato canonico del `run_id`, que desde #9 es un contrato y no una cadena opaca.

Es la clave primaria de la tabla `run` y lo que ordena las ejecuciones en la API, asi que
lo que se fija aqui es lo que otros van a dar por cierto: que ordena por tiempo, que es
unico y que se puede leer al reves para saber cuando arranco una ejecucion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from cvp.storage.run_id import RUN_ID_RE, instante_de_run_id, nuevo_run_id, validar_run_id

INICIO = datetime(2026, 8, 24, 14, 20, 18, tzinfo=UTC)


def test_el_formato_es_instante_utc_mas_sufijo_aleatorio() -> None:
    assert RUN_ID_RE.match("20260824T142018Z-2808cc47")
    generado = nuevo_run_id(INICIO)
    assert generado.startswith("20260824T142018Z-")
    assert RUN_ID_RE.match(generado)


def test_el_instante_sale_del_reloj_que_se_le_pasa_y_no_de_datetime_now() -> None:
    """El reloj se inyecta como en el resto del sistema: asi un test puede fijar el id."""
    assert nuevo_run_id(INICIO).split("-")[0] == "20260824T142018Z"


def test_una_marca_sin_zona_no_vale_para_generar_un_run_id() -> None:
    with pytest.raises(ValueError, match="zona horaria"):
        nuevo_run_id(datetime(2026, 8, 24, 14, 20, 18))


def test_una_marca_en_otra_zona_se_convierte_a_utc() -> None:
    """Dos workers en husos distintos tienen que producir ids comparables entre si."""
    en_madrid = INICIO.astimezone(timezone(timedelta(hours=2)))
    assert nuevo_run_id(en_madrid).split("-")[0] == "20260824T142018Z"


def test_ordena_lexicograficamente_por_tiempo() -> None:
    """Es lo que permite `ORDER BY id DESC` sin una columna ni un indice de mas."""
    marcas = [
        INICIO - timedelta(days=400),
        INICIO - timedelta(hours=3),
        INICIO - timedelta(seconds=1),
        INICIO,
    ]
    ids = [nuevo_run_id(m) for m in marcas]
    assert ids == sorted(ids)


def test_dos_arranques_en_el_mismo_segundo_no_chocan() -> None:
    """Dos workers del mismo proyecto no se coordinan: por eso el sufijo es aleatorio."""
    ids = {nuevo_run_id(INICIO) for _ in range(200)}
    assert len(ids) == 200


def test_se_puede_leer_al_reves_para_saber_cuando_arranco() -> None:
    """Es lo que rellena `run.started_at` sin que el sink consulte ningun reloj."""
    assert instante_de_run_id(nuevo_run_id(INICIO)) == INICIO


@pytest.mark.parametrize(
    "invalido",
    [
        "",
        "20260824T142018Z",  # sin sufijo
        "20260824T142018Z-2808CC47",  # el sufijo va en minusculas
        "20260824T142018-2808cc47",  # sin la Z, y entonces no se sabe en que zona esta
        "2026-08-24T14:20:18Z-2808cc47",  # ISO con separadores: no ordena igual de ancho
        "20260824T142018Z-2808cc4",  # sufijo corto
        "20260899T142018Z-2808cc47",  # encaja en la expresion y no es una fecha
    ],
    ids=[
        "vacio",
        "sin-sufijo",
        "sufijo-en-mayusculas",
        "sin-zona",
        "iso-con-separadores",
        "sufijo-corto",
        "fecha-imposible",
    ],
)
def test_lo_que_no_encaja_en_el_formato_se_rechaza(invalido: str) -> None:
    """Se valida al escribir en la base, no solo al generar: una clave con otra forma se
    colaria sin romper nada hasta que alguien mirase el orden de las ejecuciones."""
    with pytest.raises(ValueError, match="run_id invalido"):
        validar_run_id(invalido)


def test_un_run_id_valido_se_devuelve_tal_cual() -> None:
    assert validar_run_id("20260824T142018Z-2808cc47") == "20260824T142018Z-2808cc47"
