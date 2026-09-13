"""Los analizadores: la logica de negocio, y el unico bloque que un proyecto nuevo suele tocar.

Importar este paquete registra los analizadores en `cvp.core.registry.ANALYZERS`, que es
lo que permite que un `type: traffic_flow` del YAML se resuelva a una clase.

Lo que comparten todos, y que un analizador nuevo debe reutilizar en vez de copiar:

- `cvp.analyzers.geometry` — de fracciones a pixeles desde el primer fotograma, y el lado
  de un punto respecto a una linea.
- `cvp.analyzers.drawing` — las primitivas de `annotate`, que nunca pintan sobre el array
  que reciben.
"""

from __future__ import annotations

__all__: list[str] = []
