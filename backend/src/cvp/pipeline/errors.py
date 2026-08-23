"""El unico error que sale de la carga de configuracion.

Vive aparte de `config.py` por el mismo motivo que `cvp.core.errors` vive aparte de
`registry.py`: quien lo captura (el CLI, el runner) no tiene por que importar Pydantic ni
el modulo de esquemas entero solo para escribir un `except`.

Es subclase de `ValueError` como las dos excepciones de `cvp.core`: un `project.yaml` mal
escrito es un valor invalido, y quien ya captura `ValueError` no necesita cambiar nada.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["ConfigError"]


class ConfigError(ValueError):
    """La configuracion de un proyecto no es valida, con **todos** los motivos.

    El texto lo lee una persona a las cuatro de la manana, asi que tiene una forma fija:
    una cabecera con la ruta del fichero tal y como se pidio y cuantos errores hay, y
    despues una linea por error con la ruta del campo, que se esperaba y que habia.

        tests/fixtures/config/roto-tres-errores.yaml: 3 errores de configuración
          - analyzers[1].hysteresis.to_free_s: campo obligatorio que falta
          - detector.conf: debe estar entre 0 y 1, y vale 1.4
          - source.target_fps: debe ser > 0, y vale -1.0

    `problems` guarda esas lineas sueltas ademas del bloque entero. Es lo que permite que
    un test afirme sobre un error concreto sin depender del orden ni del resto del texto.
    """

    def __init__(self, source: str, problems: Iterable[str]) -> None:
        problemas = tuple(problems)
        plural = "error" if len(problemas) == 1 else "errores"
        cabecera = f"{source}: {len(problemas)} {plural} de configuración"
        super().__init__("\n".join([cabecera, *(f"  - {p}" for p in problemas)]))
        self.source = source
        self.problems: tuple[str, ...] = problemas
