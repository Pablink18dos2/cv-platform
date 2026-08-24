"""Los errores del pipeline: la carga de configuracion y la ejecucion.

Viven aparte de `config.py` y de `runner.py` por el mismo motivo que `cvp.core.errors`
vive aparte de `registry.py`: quien los captura (el CLI, el runner) no tiene por que
importar Pydantic, ni OpenCV, ni el modulo de esquemas entero solo para escribir un
`except`. Este modulo no importa nada mas que la biblioteca estandar.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["AbortRequested", "ConfigError", "PipelineError"]


class AbortRequested(BaseException):
    """El usuario ha pedido abortar —segundo Ctrl+C— y no quiere esperar al apagado.

    **Deriva de `BaseException` y no de `KeyboardInterrupt`, y ese es todo el punto.** El
    bucle de `run()` captura `KeyboardInterrupt` a proposito, para convertir el primer
    Ctrl+C en un apagado ordenado con codigo 0. Si el aviso de aborto fuera un
    `KeyboardInterrupt` —que es lo que parece natural, porque es lo que lanza Python— se lo
    tragaria ese mismo `except` y el segundo Ctrl+C acabaria siendo otro apagado ordenado:
    justo el fallo que tenia la primera version de este runner, donde el aborto solo
    funcionaba si la senal caia dentro del `finally`, o sea en el caso raro y no en el que
    hace el usuario.

    Por el mismo motivo tampoco deriva de `Exception`: ningun `except Exception` de los que
    protegen un sink roto o el cierre de la fuente debe poder detenerla. Solo el CLI, que la
    traduce a codigo 130, y quien escriba `except AbortRequested` con toda la intencion.
    """


class PipelineError(RuntimeError):
    """El pipeline no ha podido arrancar o no ha podido seguir.

    Es de ejecucion, no de configuracion: el YAML estaba bien y aun asi la fuente no
    contesta, o la clase registrada no sabe construirse desde su configuracion. Por eso es
    un `RuntimeError` y no un `ValueError`, y por eso el CLI le da un codigo de salida
    distinto al de un `project.yaml` mal escrito: no se arregla editando el fichero.
    """


class ConfigError(ValueError):
    """La configuracion de un proyecto no es valida, con **todos** los motivos.

    Es subclase de `ValueError` como las dos excepciones de `cvp.core`: un `project.yaml`
    mal escrito es un valor invalido, y quien ya captura `ValueError` no necesita cambiar
    nada.

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
