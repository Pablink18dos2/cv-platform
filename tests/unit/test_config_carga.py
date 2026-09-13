"""La carga: de donde se lee, que se devuelve y como suena el error.

El catalogo de ficheros rotos vive en `tests/fixtures/config/` y se recorre entero: un
fixture roto sin su caso en la tabla de abajo hace fallar la suite, para que nadie deje un
fichero huerfano que no comprueba nada.
"""

from __future__ import annotations

import copy
import inspect
from pathlib import Path

import pytest
import yaml

from cvp.pipeline import ConfigError, ProjectConfig, load_project_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
VALIDO = FIXTURES / "valido.yaml"

# Fixture roto -> un trozo del mensaje que tiene que salir, normalmente la ruta del campo
# culpable. Es la tabla que impide que un fixture nuevo se quede sin comprobar.
ROTOS = {
    "roto-backoff-decreciente.yaml": "source.reconnect.backoff_s:",
    "roto-backoff-vacio.yaml": "source.reconnect.backoff_s:",
    "roto-campo-desconocido.yaml": "source.targt_fps:",
    "roto-campos-derivados.yaml": "config_path:",
    "roto-clases-duplicadas.yaml": "detector.classes:",
    "roto-clases-vacias.yaml": "detector.classes:",
    "roto-claves-duplicadas.yaml": "analyzers: clave duplicada",
    "roto-entorno-ausente.yaml": "source.url: la variable de entorno YT_URL no está definida",
    "roto-entorno-sin-cerrar.yaml": "source.url:",
    "roto-every-n-frames-cero.yaml": "analyzers[0].every_n_frames:",
    "roto-id-invalido.yaml": "id:",
    "roto-imgsz-no-multiplo.yaml": "detector.imgsz:",
    "roto-linea-longitud-cero.yaml": "analyzers[0].lines[0]:",
    "roto-lineas-id-duplicado.yaml": "analyzers[0]:",
    "roto-max-retries-negativo.yaml": "source.reconnect.max_retries:",
    "roto-name-vacio.yaml": "name:",
    "roto-nombres-duplicados.yaml": "analyzers[1].name:",
    "roto-punto-forma.yaml": "analyzers[0].lines[0].p1:",
    "roto-punto-fuera-de-rango.yaml": "analyzers[0].lines[0].p2[1]:",
    "roto-raiz-cadena.yaml": "un valor suelto",
    "roto-raiz-lista.yaml": "una lista",
    "roto-refresh-demasiado-largo.yaml": "source.refresh_url_every_s:",
    "roto-roi-area-cero.yaml": "detector.roi:",
    "roto-roi-forma.yaml": "detector.roi:",
    "roto-roi-fuera-de-rango.yaml": "detector.roi[2]:",
    "roto-roi-invertida.yaml": "detector.roi:",
    "roto-rtsp-url.yaml": "source.url:",
    "roto-ruta-entrada-inexistente.yaml": "source.path:",
    "roto-ruta-salida-sin-directorio.yaml": "sinks[1].path:",
    "roto-sin-type.yaml": "source.type:",
    "roto-sinks-duplicados.yaml": "sinks[1].type:",
    "roto-tres-errores.yaml": "detector.conf:",
    "roto-type-desconocido.yaml": "detector.type:",
    "roto-vacio.yaml": "el fichero está vacío",
    "roto-yaml-invalido.yaml": "el YAML no es válido (línea 4, columna 1)",
    "roto-zones-file-inexistente.yaml": "analyzers[0].zones_file:",
}

# El proyecto mas pequeno que valida, para escribirlo en un tmp_path y mutarlo.
MINIMO = {
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
                }
            ],
        }
    ],
    "sinks": [{"type": "sqlite"}],
}


def escribir(destino: Path, **cambios: object) -> Path:
    """Escribe un `project.yaml` minimo con los cambios que pida el test."""
    documento = copy.deepcopy(MINIMO) | cambios
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(yaml.safe_dump(documento, allow_unicode=True), encoding="utf-8")
    return destino


# --- La API publica ---------------------------------------------------------------


def test_config_error_es_un_valueerror() -> None:
    assert issubclass(ConfigError, ValueError)


def test_la_firma_de_load_project_config_es_la_acordada() -> None:
    """`env` es de solo palabra clave y por defecto `os.environ`: los tests inyectan el
    entorno igual que inyectan el reloj."""
    firma = inspect.signature(load_project_config)
    assert list(firma.parameters) == ["path", "env"]
    assert firma.parameters["env"].kind is inspect.Parameter.KEYWORD_ONLY
    assert firma.parameters["env"].default is None


def test_una_configuracion_valida_devuelve_un_project_config() -> None:
    config = load_project_config(VALIDO)
    assert isinstance(config, ProjectConfig)
    assert config.id == "valido"
    assert config.source.type == "youtube_live"
    assert config.detector.type == "yolo"
    assert len(config.analyzers) == 2
    assert len(config.sinks) == 3


def test_se_carga_desde_el_fichero_yaml(tmp_path: Path) -> None:
    fichero = escribir(tmp_path / "mi-proyecto" / "project.yaml")
    config = load_project_config(fichero, env={})
    assert config.config_path == fichero.resolve()


def test_se_carga_desde_el_directorio_del_proyecto(tmp_path: Path) -> None:
    escribir(tmp_path / "mi-proyecto" / "project.yaml", id="mi-proyecto")
    config = load_project_config(tmp_path / "mi-proyecto", env={})
    assert config.config_path.name == "project.yaml"
    assert config.project_dir == (tmp_path / "mi-proyecto").resolve()


def test_un_directorio_sin_project_yaml_dice_donde_lo_busco(tmp_path: Path) -> None:
    (tmp_path / "vacio").mkdir()
    with pytest.raises(ConfigError) as error:
        load_project_config(tmp_path / "vacio", env={})
    assert str((tmp_path / "vacio").resolve()) in str(error.value)


def test_un_fichero_que_no_existe_dice_la_ruta_absoluta(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_project_config(tmp_path / "no-existe.yaml", env={})
    assert str((tmp_path / "no-existe.yaml").resolve()) in str(error.value)


def test_desde_un_directorio_el_id_tiene_que_ser_el_nombre_del_directorio(tmp_path: Path) -> None:
    escribir(tmp_path / "mi-proyecto" / "project.yaml", id="otro-id")
    with pytest.raises(ConfigError) as error:
        load_project_config(tmp_path / "mi-proyecto", env={})
    assert error.value.problems == (
        "id: el proyecto está en el directorio 'mi-proyecto' pero su id es 'otro-id'; "
        "en el dashboard se identifica por el id y en el disco por la carpeta, y que "
        "discrepen es un bug garantizado",
    )


def test_desde_un_fichero_suelto_no_se_comprueba_el_id(tmp_path: Path) -> None:
    fichero = escribir(tmp_path / "carpeta-cualquiera" / "project.yaml", id="otro-id")
    assert load_project_config(fichero, env={}).id == "otro-id"


def test_la_configuracion_es_inmutable() -> None:
    config = load_project_config(VALIDO)
    with pytest.raises(ValueError, match="frozen"):
        config.id = "otro"  # type: ignore[misc]
    with pytest.raises(ValueError, match="frozen"):
        config.detector.conf = 0.9  # type: ignore[misc]


def test_expone_los_campos_del_yaml_y_los_derivados_de_la_carga() -> None:
    config = load_project_config(VALIDO)
    for campo in ("id", "name", "source", "detector", "tracker", "analyzers", "sinks"):
        assert hasattr(config, campo)
    assert config.config_path == VALIDO.resolve()
    assert config.project_dir == FIXTURES.resolve()
    assert config.config_path.is_absolute()
    assert config.project_dir.is_absolute()


# --- Rutas ------------------------------------------------------------------------


def test_las_rutas_relativas_lo_son_al_directorio_del_project_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proyecto = tmp_path / "mi-proyecto"
    (proyecto / "media").mkdir(parents=True)
    (proyecto / "media" / "clip.mp4").write_bytes(b"")
    fichero = escribir(
        proyecto / "project.yaml",
        source={"type": "file", "path": "media/clip.mp4", "target_fps": 5},
    )
    otro = tmp_path / "otro-sitio"
    otro.mkdir()

    monkeypatch.chdir(tmp_path)
    desde_tmp = load_project_config(fichero, env={})
    monkeypatch.chdir(otro)
    desde_otro = load_project_config(fichero, env={})

    assert desde_tmp.source.path == desde_otro.source.path
    assert desde_tmp.source.path == (proyecto / "media" / "clip.mp4").resolve()


def test_una_ruta_ya_absoluta_se_respeta(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    fichero = escribir(
        tmp_path / "mi-proyecto" / "project.yaml",
        source={"type": "file", "path": str(clip), "target_fps": 5},
    )
    assert load_project_config(fichero, env={}).source.path == clip.resolve()


def test_tras_la_carga_no_queda_ninguna_ruta_relativa(tmp_path: Path) -> None:
    proyecto = tmp_path / "mi-proyecto"
    proyecto.mkdir()
    (proyecto / "clip.mp4").write_bytes(b"")
    (proyecto / "zones.json").write_text("{}", encoding="utf-8")
    fichero = escribir(
        proyecto / "project.yaml",
        source={"type": "file", "path": "clip.mp4", "target_fps": 5},
        analyzers=[{"type": "parking_occupancy", "zones_file": "zones.json"}],
        sinks=[{"type": "jsonl", "path": "salida.jsonl"}],
    )
    config = load_project_config(fichero, env={})
    rutas = [
        config.config_path,
        config.project_dir,
        config.source.path,
        config.analyzers[0].zones_file,
        config.sinks[0].path,
    ]
    assert all(ruta.is_absolute() for ruta in rutas)


def test_el_fichero_de_salida_puede_no_existir_pero_su_directorio_no(tmp_path: Path) -> None:
    proyecto = tmp_path / "mi-proyecto"
    fichero = escribir(
        proyecto / "project.yaml",
        sinks=[{"type": "jsonl", "path": "salida.jsonl"}],
    )
    config = load_project_config(fichero, env={})
    assert config.sinks[0].path == (proyecto / "salida.jsonl").resolve()
    assert not config.sinks[0].path.exists()


def test_weights_no_es_una_ruta_y_no_se_toca(tmp_path: Path) -> None:
    fichero = escribir(
        tmp_path / "mi-proyecto" / "project.yaml",
        detector={"type": "yolo", "classes": ["car"], "weights": "yolo11n.pt"},
    )
    config = load_project_config(fichero, env={})
    assert config.detector.weights == "yolo11n.pt"
    assert isinstance(config.detector.weights, str)


# --- El mensaje -------------------------------------------------------------------


def test_el_bloque_de_errores_tiene_la_forma_exacta(monkeypatch: pytest.MonkeyPatch) -> None:
    ruta = "tests/fixtures/config/roto-tres-errores.yaml"
    with pytest.raises(ConfigError) as error:
        load_project_config(FIXTURES / "roto-tres-errores.yaml", env={})
    esperado = "\n".join(
        [
            f"{FIXTURES / 'roto-tres-errores.yaml'}: 3 errores de configuración",
            "  - analyzers[1].zones_file: campo obligatorio que falta",
            "  - detector.conf: debe estar entre 0 y 1, y vale 1.4",
            "  - source.target_fps: debe ser > 0, y vale -1.0",
        ]
    )
    assert str(error.value) == esperado
    # La cabecera lleva la ruta **tal y como se pidio**, no la absoluta ni la del CWD.
    monkeypatch.chdir(FIXTURES.parents[2])
    with pytest.raises(ConfigError) as relativo:
        load_project_config(ruta, env={})
    assert str(relativo.value).startswith(f"{ruta}: 3 errores de configuración\n")


def test_se_reportan_todos_los_errores_de_una_vez_no_el_primero() -> None:
    with pytest.raises(ConfigError) as error:
        load_project_config(FIXTURES / "roto-tres-errores.yaml", env={})
    assert error.value.problems == (
        "analyzers[1].zones_file: campo obligatorio que falta",
        "detector.conf: debe estar entre 0 y 1, y vale 1.4",
        "source.target_fps: debe ser > 0, y vale -1.0",
    )


def test_una_fase_que_falla_no_deja_correr_a_la_siguiente(tmp_path: Path) -> None:
    """Con un hueco sin resolver, validar el esquema no diria nada util."""
    fichero = escribir(
        tmp_path / "mi-proyecto" / "project.yaml",
        source={"type": "youtube_live", "url": "${YT_URL}", "target_fps": -1},
    )
    with pytest.raises(ConfigError) as error:
        load_project_config(fichero, env={})
    assert error.value.problems == ("source.url: la variable de entorno YT_URL no está definida",)


def test_las_lineas_van_ordenadas_por_ruta_de_campo(tmp_path: Path) -> None:
    fichero = escribir(
        tmp_path / "mi-proyecto" / "project.yaml",
        sinks=[{"type": "sqlite", "batch_size": 0}, {"type": "jsonl"}],
        detector={"type": "yolo", "classes": ["car"], "imgsz": 700},
    )
    with pytest.raises(ConfigError) as error:
        load_project_config(fichero, env={})
    assert error.value.problems == (
        "detector.imgsz: debe ser múltiplo de 32 porque ultralytics lo redondea por su "
        "cuenta y el YAML dejaría de decir la verdad, y vale 700",
        "sinks[0].batch_size: debe ser >= 1, y vale 0",
        "sinks[1].path: campo obligatorio que falta",
    )


def test_las_claves_duplicadas_de_un_mapa_son_un_error() -> None:
    with pytest.raises(ConfigError) as error:
        load_project_config(FIXTURES / "roto-claves-duplicadas.yaml", env={})
    (problema,) = error.value.problems
    assert problema.startswith("analyzers: clave duplicada (líneas 15 y 23)")


def test_un_campo_desconocido_es_un_error_no_algo_que_se_ignora() -> None:
    with pytest.raises(ConfigError) as error:
        load_project_config(FIXTURES / "roto-campo-desconocido.yaml", env={})
    assert error.value.problems[0].startswith("source.targt_fps: campo desconocido")


def test_un_type_desconocido_dice_los_que_hay() -> None:
    with pytest.raises(ConfigError) as error:
        load_project_config(FIXTURES / "roto-type-desconocido.yaml", env={})
    assert error.value.problems == (
        "detector.type: no hay ningún detector llamado 'yolo11'. Disponibles: yolo",
    )


# --- El catalogo de rotos ---------------------------------------------------------


@pytest.mark.parametrize("nombre", sorted(ROTOS))
def test_cada_fixture_roto_lanza_configerror_nombrando_al_culpable(nombre: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_project_config(FIXTURES / nombre, env={})
    assert ROTOS[nombre] in str(error.value)
    assert error.value.problems


def test_no_hay_fixtures_rotos_huerfanos() -> None:
    en_disco = {f.name for f in FIXTURES.glob("roto-*.yaml")}
    assert en_disco == set(ROTOS), "un fixture roto sin caso en la tabla no comprueba nada"


def test_de_la_carga_solo_sale_configerror(tmp_path: Path) -> None:
    """Ni ValidationError, ni KeyError, ni FileNotFoundError, ni YAMLError."""
    rutas = [*sorted(FIXTURES.glob("roto-*.yaml")), tmp_path / "no-existe.yaml", tmp_path]
    for ruta in rutas:
        try:
            load_project_config(ruta, env={})
        except ConfigError:
            continue
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{ruta.name} ha lanzado {type(exc).__name__}: {exc}")
        else:
            pytest.fail(f"{ruta.name} no ha fallado")
