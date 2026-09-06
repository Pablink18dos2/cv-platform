"""Los sinks: donde acaba lo que producen los analizadores.

Importar este paquete registra los tres en `cvp.core.registry.SINKS`, que es lo que permite
que un `type: sqlite` del YAML se resuelva a una clase sin que el runner sepa que existen:

- `sqlite` — persistencia por lotes, con la agregacion por minuto incluida (`cvp.storage`).
- `jsonl` — una linea JSON por evento y por metrica, para depurar.
- `preview` — el ultimo fotograma en disco como JPEG, que es lo que servira el MJPEG.

Los tres implementan `Sink.write(run_id, frame, output)`, y dos de los tres ignoran el
fotograma: la decision 1 de #9 dice por que es preferible a un caso especial en el runner.
"""

from __future__ import annotations

from cvp.sinks.jsonl import JsonlSink
from cvp.sinks.preview import PreviewSink, ruta_de_preview
from cvp.sinks.sqlite import SqliteSink

__all__ = ["JsonlSink", "PreviewSink", "SqliteSink", "ruta_de_preview"]
