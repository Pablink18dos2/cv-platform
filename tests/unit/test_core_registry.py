"""El registro por nombre: lo que traduce el `type:` del YAML a una clase.

Casi todo se prueba sobre registros recien creados en el propio test. Los cinco
registros globales son estado de proceso y los bloques de verdad (issues 4 a 11) se
registraran en ellos al importarse, asi que lo que un test meta ahi se deshace al salir.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from cvp.core import (
    ANALYZERS,
    DETECTORS,
    SINKS,
    SOURCES,
    TRACKERS,
    DuplicateRegistrationError,
    UnknownTypeError,
    register_analyzer,
    register_detector,
    register_sink,
    register_source,
    register_tracker,
)
from cvp.core.registry import Registry

REGISTROS_GLOBALES = (SOURCES, DETECTORS, TRACKERS, ANALYZERS, SINKS)

# Los ocho nombres que ya usa el YAML de ejemplo de `_docs/architecture.md`.
NOMBRES_DEL_YAML = (
    "youtube_live",
    "file",
    "yolo",
    "bytetrack",
    "traffic_flow",
    "parking_occupancy",
    "sqlite",
    "preview",
)


@pytest.fixture
def registro() -> Registry[object]:
    return Registry("source")


@pytest.fixture
def registros_globales_intactos() -> Iterator[None]:
    """Deja los cinco registros globales como estaban.

    Sin esto, un test que registrara `"file"` como fuente reventaria el dia que
    `sources/file.py` haga lo mismo de verdad al importarse.
    """
    copia = [(r, dict(r._entries)) for r in REGISTROS_GLOBALES]
    yield
    for registro_global, entradas in copia:
        registro_global._entries.clear()
        registro_global._entries.update(entradas)


def _ruta(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


# --- Lo basico ------------------------------------------------------------------------


def test_el_decorador_devuelve_la_clase_sin_tocarla(registro: Registry[object]) -> None:
    @registro.register("file")
    class FuenteFalsa:
        def saluda(self) -> str:
            return "hola"

    assert registro.get("file") is FuenteFalsa
    assert FuenteFalsa().saluda() == "hola"


def test_el_registro_devuelve_la_clase_no_una_instancia(registro: Registry[object]) -> None:
    @registro.register("file")
    class FuenteFalsa:
        pass

    assert isinstance(registro.get("file"), type)


def test_names_va_ordenado_y_contains_responde(registro: Registry[object]) -> None:
    for nombre in ("yolo", "file", "rtsp"):
        registro.register(nombre)(type(nombre.capitalize(), (), {}))

    assert registro.names() == ("file", "rtsp", "yolo")
    assert "file" in registro
    assert "webcam" not in registro


# --- Duplicados -----------------------------------------------------------------------


def test_dos_clases_distintas_con_el_mismo_nombre_no_pueden_convivir(
    registro: Registry[object],
) -> None:
    @registro.register("file")
    class Primera:
        pass

    class Segunda:
        pass

    with pytest.raises(DuplicateRegistrationError, match="ya hay un source .*'file'") as error:
        registro.register("file")(Segunda)

    mensaje = str(error.value)
    assert _ruta(Primera) in mensaje
    assert _ruta(Segunda) in mensaje


def test_registrar_la_misma_clase_dos_veces_es_idempotente(registro: Registry[object]) -> None:
    @registro.register("file")
    class FuenteFalsa:
        pass

    registro.register("file")(FuenteFalsa)

    assert registro.get("file") is FuenteFalsa
    assert registro.names() == ("file",)


# --- Nombres desconocidos -------------------------------------------------------------


def test_un_nombre_que_no_existe_dice_cuales_si(registro: Registry[object]) -> None:
    registro.register("yolo")(type("Yolo", (), {}))
    registro.register("file")(type("File", (), {}))

    with pytest.raises(UnknownTypeError) as error:
        registro.get("yolo11")

    mensaje = str(error.value)
    assert "source" in mensaje
    assert "yolo11" in mensaje
    assert "file, yolo" in mensaje


def test_sobre_un_registro_vacio_el_error_no_ensena_una_lista_vacia(
    registro: Registry[object],
) -> None:
    with pytest.raises(UnknownTypeError, match="no hay ningun source registrado todavia"):
        registro.get("file")


def test_las_dos_excepciones_son_errores_de_valor() -> None:
    assert issubclass(DuplicateRegistrationError, ValueError)
    assert issubclass(UnknownTypeError, ValueError)


def test_las_dos_excepciones_viven_en_cvp_core_errors() -> None:
    from cvp.core import errors

    assert DuplicateRegistrationError is errors.DuplicateRegistrationError
    assert UnknownTypeError is errors.UnknownTypeError


# --- Nombres validos ------------------------------------------------------------------


@pytest.mark.parametrize("nombre", ["YOLO", "youtube live", "youtube-live", "", "1yolo"])
def test_un_nombre_que_no_vale_como_clave_de_yaml_se_rechaza(
    registro: Registry[object], nombre: str
) -> None:
    with pytest.raises(ValueError):
        registro.register(nombre)


@pytest.mark.parametrize("nombre", NOMBRES_DEL_YAML)
def test_los_nombres_del_yaml_de_ejemplo_son_validos(
    registro: Registry[object], nombre: str
) -> None:
    registro.register(nombre)(type("Bloque", (), {}))
    assert nombre in registro


def test_solo_se_pueden_registrar_clases(registro: Registry[object]) -> None:
    with pytest.raises(TypeError):
        registro.register("file")(lambda: None)

    class FuenteFalsa:
        pass

    with pytest.raises(TypeError):
        registro.register("file")(FuenteFalsa())


# --- Los cinco registros globales -----------------------------------------------------


def test_los_cinco_registros_son_independientes(registros_globales_intactos: None) -> None:
    @register_source("solo_una_fuente")
    class FuenteFalsa:
        pass

    assert SOURCES.get("solo_una_fuente") is FuenteFalsa
    for otro in (DETECTORS, TRACKERS, ANALYZERS, SINKS):
        with pytest.raises(UnknownTypeError):
            otro.get("solo_una_fuente")


@pytest.mark.parametrize(
    ("decorador", "registro_global", "bloque"),
    [
        (register_source, SOURCES, "source"),
        (register_detector, DETECTORS, "detector"),
        (register_tracker, TRACKERS, "tracker"),
        (register_analyzer, ANALYZERS, "analyzer"),
        (register_sink, SINKS, "sink"),
    ],
    ids=["source", "detector", "tracker", "analyzer", "sink"],
)
def test_cada_decorador_escribe_en_su_registro(
    registros_globales_intactos: None,
    decorador,
    registro_global: Registry[object],
    bloque: str,
) -> None:
    @decorador("bloque_de_prueba")
    class BloqueFalso:
        pass

    assert registro_global.get("bloque_de_prueba") is BloqueFalso
    with pytest.raises(UnknownTypeError, match=bloque):
        registro_global.get("no_registrado")
