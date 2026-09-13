"""El esquema de cada bloque: que campos hay, que valores por defecto y que se rechaza.

Los valores por defecto son parte del contrato: `extractor_args` con los clientes que
funcionan y `refresh_url_every_s` por debajo de las 6 h son lo que evita, respectivamente,
un `ERROR: This video is not available` enganoso y un hueco de datos cada seis horas.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from cvp.pipeline import CONFIG_SCHEMAS, ConfigError, ProjectConfig, load_project_config
from cvp.pipeline.config import Reconnect, YoloDetector

MINIMO: dict[str, Any] = {
    "id": "minimo",
    "name": "Proyecto mínimo",
    "source": {"type": "webcam", "target_fps": 5},
    "detector": {"type": "yolo", "classes": ["car"]},
    "tracker": {"type": "bytetrack"},
    "analyzers": [
        {
            "type": "traffic_flow",
            "lines": [
                {
                    "id": "principal",
                    "p1": [0.1, 0.2],
                    "p2": [0.8, 0.7],
                    "positive_direction": "norte",
                    "negative_direction": "sur",
                }
            ],
        }
    ],
    "sinks": [{"type": "sqlite"}],
}

DIRECTO = {
    "type": "youtube_live",
    "url": "https://www.youtube.com/watch?v=EPKWu223XEg",
    "target_fps": 5,
}


def carga(tmp_path: Path, **cambios: object) -> ProjectConfig:
    """Escribe el proyecto minimo con los cambios que pida el test y lo carga."""
    documento = copy.deepcopy(MINIMO) | cambios
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8")
    return load_project_config(fichero, env={})


def problemas(tmp_path: Path, **cambios: object) -> tuple[str, ...]:
    """Lo mismo, pero devolviendo las lineas de error en vez de la configuracion."""
    with pytest.raises(ConfigError) as error:
        carga(tmp_path, **cambios)
    return error.value.problems


# --- El registro de esquemas ------------------------------------------------------


def test_config_schemas_es_dato_publico_por_bloque_y_por_nombre() -> None:
    assert set(CONFIG_SCHEMAS) == {"source", "detector", "tracker", "analyzer", "sink"}
    assert set(CONFIG_SCHEMAS["source"]) == {"file", "youtube_live", "rtsp", "webcam"}
    assert set(CONFIG_SCHEMAS["detector"]) == {"yolo"}
    assert set(CONFIG_SCHEMAS["tracker"]) == {"bytetrack"}
    assert set(CONFIG_SCHEMAS["analyzer"]) == {"traffic_flow", "parking_occupancy"}
    assert set(CONFIG_SCHEMAS["sink"]) == {"sqlite", "preview", "jsonl"}
    for esquemas in CONFIG_SCHEMAS.values():
        for modelo in esquemas.values():
            assert issubclass(modelo, BaseModel)


def test_el_mundo_es_cerrado_un_type_sin_esquema_es_un_error(tmp_path: Path) -> None:
    assert problemas(tmp_path, source={"type": "kinect", "target_fps": 5}) == (
        "source.type: no hay ningún source llamado 'kinect'. "
        "Disponibles: file, rtsp, webcam, youtube_live",
    )


# --- id y name --------------------------------------------------------------------


def test_el_id_acepta_el_del_proyecto_real(tmp_path: Path) -> None:
    assert carga(tmp_path, id="01-parking-trafico").id == "01-parking-trafico"


@pytest.mark.parametrize("valor", ["-empieza-por-guion", "Con_Mayusculas", "con espacio", ""])
def test_un_id_que_no_encaja_en_el_patron_falla(tmp_path: Path, valor: str) -> None:
    assert problemas(tmp_path, id=valor)[0].startswith("id:")


def test_analyzers_y_sinks_pueden_estar_vacios_pero_no_faltar(tmp_path: Path) -> None:
    """`analyzers: []` y `sinks: []` son el modo con el que #8 mide el encadenado completo.

    Hasta #8 una lista vacía se rechazaba. Se cambió porque, mientras no exista ningún
    analizador implementado (#10, #11) ni ningún sink (#9), es la única configuración con
    la que `cvp run` puede arrancar, y sin arrancar no hay fps que medir. Quien escriba una
    lista vacía sin querer recibe un WARNING del runner al arrancar; **omitir el campo
    entero sigue siendo un error**, que es el descuido de verdad.
    """
    config = carga(tmp_path, analyzers=[], sinks=[])
    assert config.analyzers == []
    assert config.sinks == []

    documento = copy.deepcopy(MINIMO)
    del documento["analyzers"]
    del documento["sinks"]
    fichero = tmp_path / "project.yaml"
    fichero.write_text(yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_project_config(fichero, env={})
    assert error.value.problems == (
        "analyzers: campo obligatorio que falta",
        "sinks: campo obligatorio que falta",
    )


# --- source -----------------------------------------------------------------------


def test_target_fps_es_obligatorio_y_mayor_que_cero(tmp_path: Path) -> None:
    assert problemas(tmp_path, source={"type": "webcam"}) == (
        "source.target_fps: campo obligatorio que falta",
    )
    assert problemas(tmp_path, source={"type": "webcam", "target_fps": 0}) == (
        "source.target_fps: debe ser > 0, y vale 0",
    )


def test_la_fuente_de_fichero(tmp_path: Path) -> None:
    (tmp_path / "clip.mp4").write_bytes(b"")
    config = carga(tmp_path, source={"type": "file", "path": "clip.mp4", "target_fps": 5})
    assert config.source.loop is False
    assert config.source.path == (tmp_path / "clip.mp4").resolve()
    assert problemas(tmp_path, source={"type": "file", "target_fps": 5}) == (
        "source.path: campo obligatorio que falta",
    )


def test_la_fuente_de_youtube_trae_los_clientes_que_funcionan(tmp_path: Path) -> None:
    """Sin estos clientes yt-dlp responde `This video is not available`, que engana."""
    config = carga(tmp_path, source=DIRECTO)
    assert config.source.extractor_args == {
        "youtube": {"player_client": ["web_safari", "tv", "android"]}
    }
    assert config.source.format == "best"
    assert config.source.refresh_url_every_s == 18000
    assert config.source.reconnect.max_retries == 20


def test_escribir_extractor_args_sustituye_el_valor_por_defecto_entero(tmp_path: Path) -> None:
    """Con fusion no habria forma de quitar un cliente que YouTube haya roto."""
    config = carga(
        tmp_path,
        source=DIRECTO | {"extractor_args": {"youtube": {"player_client": ["tv"]}}},
    )
    assert config.source.extractor_args == {"youtube": {"player_client": ["tv"]}}


def test_la_url_del_directo_es_obligatoria(tmp_path: Path) -> None:
    assert problemas(tmp_path, source={"type": "youtube_live", "target_fps": 5}) == (
        "source.url: campo obligatorio que falta",
    )


@pytest.mark.parametrize("segundos", [21601, 30000])
def test_re_resolver_despues_de_las_6h_falla_explicando_por_que(
    tmp_path: Path, segundos: int
) -> None:
    (problema,) = problemas(tmp_path, source=DIRECTO | {"refresh_url_every_s": segundos})
    assert problema.startswith("source.refresh_url_every_s:")
    assert "6 h (21600 s)" in problema


def test_el_rango_de_refresh_tambien_tiene_suelo(tmp_path: Path) -> None:
    assert problemas(tmp_path, source=DIRECTO | {"refresh_url_every_s": 59})[0].startswith(
        "source.refresh_url_every_s:"
    )
    assert carga(tmp_path, source=DIRECTO | {"refresh_url_every_s": 60}).source.refresh_url_every_s


def test_rtsp_exige_el_esquema_de_la_url(tmp_path: Path) -> None:
    valida = {"type": "rtsp", "url": "rtsp://user:pass@192.168.1.10/stream", "target_fps": 2}
    assert carga(tmp_path, source=valida).source.url.startswith("rtsp://")
    assert problemas(tmp_path, source=valida | {"url": "192.168.1.10/stream"})[0].startswith(
        "source.url:"
    )


def test_la_webcam_usa_el_dispositivo_0_por_defecto(tmp_path: Path) -> None:
    assert carga(tmp_path).source.device == 0
    assert problemas(tmp_path, source={"type": "webcam", "target_fps": 5, "device": -1}) == (
        "source.device: debe ser >= 0, y vale -1",
    )


# --- reconnect --------------------------------------------------------------------


def test_los_valores_por_defecto_de_reconnect() -> None:
    reconnect = Reconnect()
    assert reconnect.max_retries == 20
    assert reconnect.backoff_s == [1, 2, 5, 15, 30, 60]


def test_reintentar_siempre_o_no_reintentar_son_configuraciones_legitimas(tmp_path: Path) -> None:
    assert carga(tmp_path, source=DIRECTO | {"reconnect": {"max_retries": None}}) is not None
    assert carga(tmp_path, source=DIRECTO | {"reconnect": {"max_retries": 0}}) is not None


@pytest.mark.parametrize(
    "backoff",
    [[], [0, 1, 2], [-1], [1, 2, 60, 30]],
    ids=["vacia", "con-cero", "negativa", "decreciente"],
)
def test_un_backoff_invalido_falla(tmp_path: Path, backoff: list[float]) -> None:
    assert problemas(tmp_path, source=DIRECTO | {"reconnect": {"backoff_s": backoff}})[
        0
    ].startswith("source.reconnect.backoff_s:")


def test_el_docstring_de_reconnect_fija_la_semantica_que_hereda_el_connector() -> None:
    """Si hay mas reintentos que elementos, se repite el ultimo. Sin esto, cada connector
    se lo inventaria de otra manera."""
    assert "repite el ultimo" in " ".join((Reconnect.__doc__ or "").split())


# --- detector y tracker -----------------------------------------------------------


def test_los_valores_por_defecto_del_detector(tmp_path: Path) -> None:
    config = carga(tmp_path)
    assert config.detector.weights == "yolo11n.pt"
    assert config.detector.imgsz == 640
    assert config.detector.conf == 0.35
    assert config.detector.roi is None


@pytest.mark.parametrize("imgsz", [700, 319, 1281, 300])
def test_un_imgsz_que_no_es_multiplo_de_32_o_se_sale_del_rango_falla(
    tmp_path: Path, imgsz: int
) -> None:
    assert problemas(tmp_path, detector={"type": "yolo", "classes": ["car"], "imgsz": imgsz})[
        0
    ].startswith("detector.imgsz:")


@pytest.mark.parametrize("conf", [0, 1.4, -0.1])
def test_una_confianza_fuera_de_0_1_falla(tmp_path: Path, conf: float) -> None:
    # El valor sale ya convertido a float: es el que ha llegado al campo.
    assert problemas(tmp_path, detector={"type": "yolo", "classes": ["car"], "conf": conf}) == (
        f"detector.conf: debe estar entre 0 y 1, y vale {float(conf)!r}",
    )


@pytest.mark.parametrize(
    "classes",
    [[], ["car", "car"], ["car", ""]],
    ids=["vacia", "duplicada", "con-cadena-vacia"],
)
def test_las_clases_del_detector(tmp_path: Path, classes: list[str]) -> None:
    assert problemas(tmp_path, detector={"type": "yolo", "classes": classes})[0].startswith(
        "detector.classes:"
    )


def test_las_clases_son_obligatorias(tmp_path: Path) -> None:
    assert problemas(tmp_path, detector={"type": "yolo"}) == (
        "detector.classes: campo obligatorio que falta",
    )


def test_el_docstring_del_detector_deja_claro_que_weights_no_es_una_ruta() -> None:
    documentacion = " ".join((YoloDetector.__doc__ or "").split())
    assert "no es un campo de ruta" in documentacion
    assert "CVP_MODELS_DIR" in documentacion


def test_el_tracker_no_tiene_campos_obligatorios_pero_si_rechaza_inventos(tmp_path: Path) -> None:
    assert carga(tmp_path, tracker={"type": "bytetrack"}).tracker.type == "bytetrack"
    assert problemas(tmp_path, tracker={"type": "bytetrack", "track_buffer": 30}) == (
        "tracker.track_buffer: campo desconocido; en un project.yaml casi siempre es una errata",
    )


# --- analyzers --------------------------------------------------------------------


def test_el_nombre_efectivo_de_un_analizador_es_su_type_si_no_se_escribe(tmp_path: Path) -> None:
    config = carga(tmp_path)
    assert config.analyzers[0].name is None
    assert config.analyzers[0].effective_name == "traffic_flow"
    assert config.analyzers[0].every_n_frames == 1


def test_dos_analizadores_con_el_mismo_nombre_efectivo_fallan(tmp_path: Path) -> None:
    (tmp_path / "zones.json").write_text("{}", encoding="utf-8")
    analyzers = [
        MINIMO["analyzers"][0] | {"name": "vehiculos"},
        {"type": "parking_occupancy", "zones_file": "zones.json", "name": "vehiculos"},
    ]
    assert problemas(tmp_path, analyzers=analyzers)[0].startswith("analyzers[1].name:")


def test_dos_analizadores_del_mismo_tipo_con_nombres_distintos_conviven(tmp_path: Path) -> None:
    analyzers = [
        MINIMO["analyzers"][0] | {"name": "entrada"},
        MINIMO["analyzers"][0] | {"name": "salida"},
    ]
    config = carga(tmp_path, analyzers=analyzers)
    assert [a.effective_name for a in config.analyzers] == ["entrada", "salida"]


def test_every_n_frames_es_al_menos_1(tmp_path: Path) -> None:
    assert problemas(tmp_path, analyzers=[MINIMO["analyzers"][0] | {"every_n_frames": 0}]) == (
        "analyzers[0].every_n_frames: debe ser >= 1, y vale 0",
    )


def test_traffic_flow_necesita_al_menos_una_linea(tmp_path: Path) -> None:
    assert problemas(tmp_path, analyzers=[{"type": "traffic_flow", "lines": []}])[0].startswith(
        "analyzers[0].lines:"
    )


def test_los_id_de_las_lineas_son_unicos_dentro_del_analizador(tmp_path: Path) -> None:
    linea = MINIMO["analyzers"][0]["lines"][0]
    analyzers = [{"type": "traffic_flow", "lines": [linea, dict(linea)]}]
    assert "principal" in problemas(tmp_path, analyzers=analyzers)[0]


def test_una_linea_de_longitud_cero_no_la_cruza_nadie(tmp_path: Path) -> None:
    linea = MINIMO["analyzers"][0]["lines"][0] | {"p2": [0.1, 0.2]}
    assert problemas(tmp_path, analyzers=[{"type": "traffic_flow", "lines": [linea]}])[
        0
    ].startswith("analyzers[0].lines[0]:")


def test_una_linea_lleva_los_dos_sentidos_con_su_nombre(tmp_path: Path) -> None:
    """El dashboard necesita dos series por linea, con sus nombres: "hacia el norte" y
    "hacia el sur". Inventar un `"no-norte"` en las metricas seria trasladar al frontal
    una decision que corresponde al YAML (decision 2 de #10)."""
    linea = carga(tmp_path).analyzers[0].lines[0]
    assert (linea.positive_direction, linea.negative_direction) == ("norte", "sur")


def test_negative_direction_es_obligatorio(tmp_path: Path) -> None:
    linea = dict(MINIMO["analyzers"][0]["lines"][0])
    del linea["negative_direction"]
    assert problemas(tmp_path, analyzers=[{"type": "traffic_flow", "lines": [linea]}]) == (
        "analyzers[0].lines[0].negative_direction: campo obligatorio que falta",
    )


@pytest.mark.parametrize("sentido", ["norte", ""])
def test_los_dos_sentidos_de_una_linea_no_pueden_coincidir_ni_estar_vacios(
    tmp_path: Path, sentido: str
) -> None:
    linea = MINIMO["analyzers"][0]["lines"][0] | {"negative_direction": sentido}
    (problema,) = problemas(tmp_path, analyzers=[{"type": "traffic_flow", "lines": [linea]}])
    if sentido:
        assert problema.startswith(
            "analyzers[0].lines[0]: positive_direction y negative_direction son el mismo sentido"
        )
    else:
        assert problema.startswith("analyzers[0].lines[0].negative_direction: no puede estar vacío")


def test_parking_occupancy_y_su_histeresis(tmp_path: Path) -> None:
    (tmp_path / "zones.json").write_text("{}", encoding="utf-8")
    config = carga(tmp_path, analyzers=[{"type": "parking_occupancy", "zones_file": "zones.json"}])
    analizador = config.analyzers[0]
    assert analizador.occupied_iou == 0.35
    assert analizador.hysteresis.to_occupied_s == 6
    assert analizador.hysteresis.to_free_s == 10
    assert analizador.zones_file == (tmp_path / "zones.json").resolve()


@pytest.mark.parametrize(
    ("escrito", "esperado"),
    [
        ({"to_occupied_s": 8}, (8.0, 10.0)),
        ({"to_free_s": 25}, (6.0, 25.0)),
        ({"to_occupied_s": 8, "to_free_s": 25}, (8.0, 25.0)),
    ],
    ids=["solo-to-occupied", "solo-to-free", "los-dos"],
)
def test_los_dos_umbrales_de_la_histeresis_son_independientes(
    tmp_path: Path, escrito: dict[str, float], esperado: tuple[float, float]
) -> None:
    """Escribir uno solo es legal y deja el otro en su valor por defecto: los dos umbrales
    son asimetricos a proposito y ajustar uno no obliga a repetir el otro."""
    (tmp_path / "zones.json").write_text("{}", encoding="utf-8")
    analyzers = [{"type": "parking_occupancy", "zones_file": "zones.json", "hysteresis": escrito}]
    histeresis = carga(tmp_path, analyzers=analyzers).analyzers[0].hysteresis
    assert (histeresis.to_occupied_s, histeresis.to_free_s) == esperado


def test_un_umbral_de_histeresis_no_positivo_sigue_fallando(tmp_path: Path) -> None:
    """Que el campo sea opcional no lo hace menos exigente cuando se escribe."""
    (tmp_path / "zones.json").write_text("{}", encoding="utf-8")
    analyzers = [
        {
            "type": "parking_occupancy",
            "zones_file": "zones.json",
            "hysteresis": {"to_free_s": 0},
        }
    ]
    assert problemas(tmp_path, analyzers=analyzers) == (
        "analyzers[0].hysteresis.to_free_s: debe ser > 0, y vale 0",
    )


def test_el_zones_file_del_aparcamiento_tiene_que_existir(tmp_path: Path) -> None:
    analyzers = [{"type": "parking_occupancy", "zones_file": "zones.json"}]
    (problema,) = problemas(tmp_path, analyzers=analyzers)
    assert problema.startswith("analyzers[0].zones_file: no existe el fichero de entrada")
    assert str(tmp_path.resolve() / "zones.json") in problema


# --- sinks ------------------------------------------------------------------------


def test_los_valores_por_defecto_de_los_sinks(tmp_path: Path) -> None:
    config = carga(tmp_path, sinks=[{"type": "sqlite"}, {"type": "preview"}])
    assert config.sinks[0].url == "sqlite:///data/cvp.sqlite3"
    assert (config.sinks[0].batch_size, config.sinks[0].max_interval_s) == (200, 5.0)
    # El preview no tiene nada que ajustar: en disco solo existe el ultimo fotograma.
    assert set(type(config.sinks[1]).model_fields) == {"type"}


def test_el_preview_ya_no_acepta_max_frames(tmp_path: Path) -> None:
    """Era el tamano del ring buffer en memoria que #9 sustituyo por un fichero. Se retiro
    en vez de dejarlo inerte: un campo que `cvp validate` bendice y que no hace nada es una
    trampa para quien lo escriba creyendo que configura algo."""
    (problema,) = problemas(tmp_path, sinks=[{"type": "preview", "max_frames": 2}])
    assert problema.startswith("sinks[0].max_frames: campo desconocido")


def test_el_jsonl_necesita_una_ruta(tmp_path: Path) -> None:
    assert problemas(tmp_path, sinks=[{"type": "jsonl"}]) == (
        "sinks[0].path: campo obligatorio que falta",
    )


def test_repetir_el_mismo_tipo_de_sink_es_un_error(tmp_path: Path) -> None:
    (problema,) = problemas(tmp_path, sinks=[{"type": "sqlite"}, {"type": "sqlite"}])
    assert problema.startswith("sinks[1].type: ya hay un sink 'sqlite' en sinks[0]")
