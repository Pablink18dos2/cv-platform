"""El test que demuestra que el andamio esta en pie: el paquete se importa y el CLI responde."""

from __future__ import annotations

import pytest

import cvp
from cvp.cli import build_parser, main


def test_el_paquete_expone_su_version() -> None:
    assert cvp.__version__


def test_el_cli_muestra_la_version_y_termina_sin_error() -> None:
    with pytest.raises(SystemExit) as salida:
        build_parser().parse_args(["--version"])
    assert salida.value.code == 0


def test_el_cli_sin_argumentos_devuelve_cero() -> None:
    assert main([]) == 0
