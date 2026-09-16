"""El analizador de aparcamiento: cuantas plazas estan libres de las que se vigilan, plaza a
plaza, sin parpadear cuando un autobus tapa media fila.

Es el segundo analizador y sigue el molde de `traffic_flow` (#10). Las tres decisiones que
lo forman estan argumentadas en #11 y van aqui arriba:

1. **La ocupacion se calcula sobre las detecciones, no sobre los tracks.** Una plaza esta
   ocupada aunque el coche de dentro no tenga identidad confirmada, y el tracker
   **descarta lo que no confirma**: en la escena del proyecto 01 sigue 5,4 de los ~14
   vehiculos por fotograma que el detector ve (`_docs/rendimiento.md`, #7). Sobre tracks
   la ocupacion saldria sistematicamente baja y nada lo delataria. Presencia e identidad
   son dos preguntas distintas; este analizador responde a la primera y por eso `process`
   recibe las dos listas y usa solo `detections`.

2. **Un coche ocupa como mucho una plaza, y es la que mas cubre.** `occupied_iou` es IoU de
   verdad —interseccion partido por union, en pixeles, rasterizando la plaza con
   `cv2.fillPoly` (`cvp.analyzers.geometry`)— y cada deteccion se asigna **solo** a la
   plaza de mayor IoU si supera el umbral. Sin eso un todoterreno grande, o una caja que
   la perspectiva estira, ocuparia dos plazas y el conteo de libres saldria bajo por otro
   camino distinto al de los tracks. El estado es por plaza: dos detecciones en la misma
   la ocupan una vez.

3. **La histeresis es en tiempo de video, y cualquier interrupcion la reinicia.** Una
   plaza pasa a ocupada cuando lleva `to_occupied_s` **seguidos** viendose ocupada, y a
   libre cuando lleva `to_free_s` seguidos viendose libre; el tiempo sale de
   `frame.timestamp` y nunca de contar fotogramas, porque la cadencia es variable (#23).
   Una sola observacion en contra reinicia la cuenta: es lo que hace que un autobus que
   tapa la fila durante `to_free_s - 1` segundos no libere nada, y que un coche que cruza
   por delante durante `to_occupied_s - 1` no ocupe nada. Los dos umbrales son asimetricos
   a proposito: liberar una plaza que sigue ocupada es peor error que tardar en marcarla.

Y una cuarta, sobre el arranque: **la primera observacion de cada plaza es su estado
inicial, sin evento, y desde ahi se aplica la misma histeresis que en regimen.** Al arrancar
el worker, una plaza ya ocupada cuenta como ocupada desde el primer fotograma y no genera
un `space_occupied` por ello; si un autobus la tapa en los segundos siguientes, `to_free_s`
la protege igual que en regimen; y una deteccion que parpadea se absorbe igual. La primera
version tenia un estado "todavia no se" que esperaba `to_occupied_s` antes de decidir, y QA
midio que era **peor** que el regimen: sin proteccion del autobus durante el arranque, una
deteccion que parpadeaba no salia nunca del limbo, y los ceros que no se emitian por plaza
si entraban en `spaces_occupied`, asi que la global y la suma de plazas discrepaban en
SQLite. El criterio se corrigio el 2026-09-16 y desde entonces hay un invariante: en cada
`process()`, `spaces_free + spaces_occupied == spaces_total` y `spaces_occupied` es la suma
de los `zone_occupied` de ese mismo `process()`. El unico "todavia no se" que queda es antes
del primer fotograma (`estado_de` devuelve `None`, `annotate` pinta gris), y ahi no se emite
ninguna metrica.

Todos los timestamps salen de `frame.timestamp`. Aqui no hay reloj.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from cvp.analyzers.drawing import (
    AMBAR,
    BLANCO,
    GRIS,
    NEGRO,
    ROJO,
    VERDE,
    Color,
    copia_para_anotar,
    dibujar_etiqueta,
    dibujar_poligono_relleno,
)
from cvp.analyzers.geometry import (
    GeometriaPorFotograma,
    MascaraDePoligono,
    Poligono,
    PoligonoEnPixeles,
    rasterizar_poligono,
)
from cvp.core import AnalyzerOutput, Detection, Event, Frame, Metric, Track
from cvp.core.registry import ANALYZERS

if TYPE_CHECKING:  # pragma: no cover - solo para el tipado
    from cvp.pipeline.config import ParkingOccupancyAnalyzer as ParkingOccupancyConfig

__all__ = ["Ocupacion", "ParkingOccupancyAnalyzer", "cargar_zonas"]

# Los nombres que salen en la base de datos y en el dashboard. Son contrato de #11.
EVENTO_OCUPADA = "space_occupied"
EVENTO_LIBERADA = "space_freed"
METRICA_LIBRES = "spaces_free"
METRICA_OCUPADAS = "spaces_occupied"
METRICA_TOTAL = "spaces_total"
METRICA_PLAZA = "zone_occupied"

# Los mismos valores por defecto que `ParkingOccupancyAnalyzer` e `Hysteresis` en
# `cvp.pipeline.config`, para que construir a mano y desde el YAML den lo mismo.
OCCUPIED_IOU_POR_DEFECTO = 0.35
TO_OCCUPIED_S_POR_DEFECTO = 6.0
TO_FREE_S_POR_DEFECTO = 10.0


class Ocupacion(Enum):
    """El estado **confirmado** de una plaza: lo que cuentan las metricas y lo que un
    evento dice que ha cambiado. No hay un tercer valor: antes del primer fotograma una
    plaza no tiene estado (`None`), y desde el primero tiene uno de estos dos."""

    LIBRE = "libre"
    OCUPADA = "ocupada"


@dataclass(slots=True)
class _EstadoDePlaza:
    """El estado de **una** plaza, con la histeresis a medias.

    `confirmado` es lo que se da por bueno y lo que cuentan las metricas; es `None` solo
    antes de la primera observacion. `candidato` es lo que se esta observando ultimamente
    (`True` = con coche) y `desde` el timestamp del primer fotograma de esa racha: la
    cuenta de la histeresis es `t - desde`.
    """

    confirmado: Ocupacion | None = None
    candidato: bool | None = None
    desde: datetime | None = None

    @property
    def en_espera(self) -> bool:
        """Con una racha en contra del estado confirmado, a medias: lo que se pinta en ambar."""
        return self.confirmado is not None and self.candidato != (
            self.confirmado is Ocupacion.OCUPADA
        )

    def observar(
        self, ocupada: bool, t: datetime, *, to_occupied_s: float, to_free_s: float
    ) -> tuple[Ocupacion, Ocupacion] | None:
        """Registra una observacion en el instante `t` y devuelve `(anterior, nuevo)` si el
        estado confirmado cambia.

        **La primera observacion es el estado inicial**, sin espera y sin devolver cambio:
        no viene de ninguna parte. A partir de ahi, la cuenta es `t - desde`, con `desde`
        el primer fotograma de la racha actual, y se reinicia en cuanto la observacion
        cambia: **una sola en contra vuelve a poner `desde` en ahora**. No se cuentan
        fotogramas en ningun sitio.

        Los umbrales: `to_occupied_s` para pasar a ocupada y `to_free_s` para pasar a
        libre, desde el primer fotograma, sin caso especial de arranque. Se cruza con `>=`:
        una racha de exactamente `to_occupied_s` segundos ocupa.
        """
        objetivo = Ocupacion.OCUPADA if ocupada else Ocupacion.LIBRE
        if self.confirmado is None:
            self.confirmado, self.candidato, self.desde = objetivo, ocupada, t
            return None
        if self.candidato != ocupada:
            self.candidato, self.desde = ocupada, t
        if objetivo is self.confirmado:
            return None
        assert self.desde is not None  # fijado con la primera observacion
        umbral = to_occupied_s if ocupada else to_free_s
        if (t - self.desde).total_seconds() < umbral:
            return None
        anterior, self.confirmado = self.confirmado, objetivo
        return (anterior, objetivo)


@dataclass(frozen=True, slots=True)
class _PlazaEnPixeles:
    """Una plaza convertida a la resolucion del fotograma: el poligono y su mascara."""

    poligono: PoligonoEnPixeles
    mascara: MascaraDePoligono


def cargar_zonas(ruta: Path | str) -> tuple[Poligono, ...]:
    """Lee y valida un `zones.json`. Es el formato que #14 escribe y #16 edita:

    ```json
    { "zones": [ { "id": "d01", "polygon": [[0.62, 0.58], [0.68, 0.56], [0.71, 0.63]] } ] }
    ```

    Coordenadas en fracciones 0-1, al menos 3 puntos por poligono, `id` unicos y no vacios,
    y al menos una zona. Cualquier otra cosa es `ValueError` **al construir el analizador**,
    con el fichero, la zona (`zones[i]`, y su `id` si lo tiene) y el campo que esta mal.
    Las claves que sobren (un `comment`, metadatos del editor) se ignoran.
    """
    ruta = Path(ruta)
    try:
        # `utf-8-sig`: UTF-8 con o sin BOM. El BOM es lo que escriben `Set-Content
        # -Encoding utf8` y el Bloc de notas en Windows, y `json.loads` lo rechaza.
        texto = ruta.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"{ruta}: no se puede leer el fichero de zonas: {exc}") from exc
    except UnicodeDecodeError as exc:
        # No es un `OSError`: el fichero se lee y no esta en UTF-8. Es lo que escribe
        # `Out-File` en PowerShell 5.1 por defecto (UTF-16), y sin la ruta en el mensaje
        # no se sabe que fichero es.
        raise ValueError(f"{ruta}: el fichero de zonas no esta en UTF-8: {exc}") from exc
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{ruta}: el JSON no es valido (linea {exc.lineno}, columna {exc.colno}): {exc.msg}"
        ) from exc
    if not isinstance(datos, dict):
        raise ValueError(
            f"{ruta}: la raiz tiene que ser un objeto con la clave 'zones', no "
            f"{type(datos).__name__}"
        )
    if "zones" not in datos:
        otras = ", ".join(repr(k) for k in sorted(datos))
        raise ValueError(
            f"{ruta}: falta la clave 'zones'" + (f" (las que hay: {otras})" if otras else "")
        )
    zonas = datos["zones"]
    if not isinstance(zonas, list):
        raise ValueError(f"{ruta}: 'zones' tiene que ser una lista, no {type(zonas).__name__}")
    if not zonas:
        raise ValueError(f"{ruta}: 'zones' esta vacia: sin plazas no hay nada que medir")

    poligonos: list[Poligono] = []
    vistos: dict[str, int] = {}
    for i, zona in enumerate(zonas):
        donde = f"{ruta}: zones[{i}]"
        if not isinstance(zona, dict):
            raise ValueError(
                f"{donde}: cada zona es un objeto con 'id' y 'polygon', no {type(zona).__name__}"
            )
        if "id" not in zona:
            raise ValueError(f"{donde}: falta 'id'")
        id_ = zona["id"]
        if not isinstance(id_, str) or not id_.strip():
            raise ValueError(f"{donde}: 'id' esta vacio, y es lo que etiqueta la plaza")
        if id_ in vistos:
            raise ValueError(
                f"{donde}: el id {id_!r} esta repetido (ya esta en zones[{vistos[id_]}]), y es "
                "lo que distingue una plaza de otra"
            )
        vistos[id_] = i
        donde = f"{donde} (id {id_!r})"
        if "polygon" not in zona:
            raise ValueError(f"{donde}: falta 'polygon'")
        polygon = zona["polygon"]
        if not isinstance(polygon, list) or not all(_es_par_de_numeros(p) for p in polygon):
            raise ValueError(
                f"{donde}: 'polygon' es una lista de pares [x, y] en fracciones 0-1 del fotograma"
            )
        try:
            poligonos.append(
                Poligono(id=id_, puntos=tuple((float(x), float(y)) for x, y in polygon))
            )
        except ValueError as exc:
            raise ValueError(f"{donde}: 'polygon': {exc}") from exc
    return tuple(poligonos)


def _es_par_de_numeros(valor: object) -> bool:
    return (
        isinstance(valor, list | tuple)
        and len(valor) == 2
        and all(isinstance(c, int | float) and not isinstance(c, bool) for c in valor)
    )


@ANALYZERS.register("parking_occupancy")
class ParkingOccupancyAnalyzer:
    """Dice cuantas plazas estan libres de las M que se vigilan, plaza a plaza.

    Emite `Event("space_occupied")` y `Event("space_freed")` con `{"zone": id}` cuando el
    estado confirmado de una plaza cambia, y en cada `process()` tres metricas sin
    etiquetas —`spaces_free`, `spaces_occupied` y `spaces_total`— mas una
    `Metric("zone_occupied", 0|1, labels={"zone": id})` por plaza. Las cuatro cuadran
    siempre: `spaces_free + spaces_occupied == spaces_total` y `spaces_occupied` es la suma
    de los `zone_occupied` del mismo `process()`. `spaces_total` es lo que permite al
    dashboard decir **"N libres de M monitorizadas"** y no "de la calle"; la media ponderada
    por tiempo de `zone_occupied` en `metric_minute` es la fraccion del minuto que esa plaza
    estuvo ocupada.

    **Lo que ya se sabe de la escena del proyecto 01** (`projects/01-parking-trafico/fuente.md`):
    las plazas del fondo **no se vigilan porque no se ven** —los coches son manchas de
    pocos pixeles que se tapan entre si—, solo las del primer plano y el plano medio, y
    aun ahi **la ocupacion es una aproximacion**: una caja de deteccion no es una plaza,
    un vehiculo mal aparcado o una perspectiva que estira la caja mueven el IoU, y los
    arboles de la izquierda tapan parte de esa fila. El numero que sale es "de las M
    monitorizadas", nunca "de la calle".

    **Ignora los tracks a proposito.** El tracker descarta lo que no confirma —5,4 de ~14
    vehiculos por fotograma en esta escena, #7— y una plaza esta ocupada aunque el coche
    de dentro no tenga identidad confirmada. Usar tracks daria una ocupacion
    sistematicamente baja sin que nada lo delatara; las detecciones crudas son lo que
    responde a "hay algo ahi".

    `name` es el del YAML (`effective_name`) y es lo que va en `analyzer` de cada evento y
    de cada metrica. `occupied_iou` es el IoU minimo entre la caja y la plaza;
    `to_occupied_s` y `to_free_s`, la histeresis en **segundos de video**.
    """

    def __init__(
        self,
        zones: Iterable[Poligono],
        *,
        name: str = "parking_occupancy",
        every_n_frames: int = 1,
        occupied_iou: float = OCCUPIED_IOU_POR_DEFECTO,
        to_occupied_s: float = TO_OCCUPIED_S_POR_DEFECTO,
        to_free_s: float = TO_FREE_S_POR_DEFECTO,
    ) -> None:
        if not name.strip():
            raise ValueError("name no puede estar vacio: es la columna `analyzer` de cada serie")
        if every_n_frames < 1:
            raise ValueError(f"every_n_frames es una cadencia >= 1, no {every_n_frames!r}")
        if not 0.0 < occupied_iou <= 1.0:
            raise ValueError(f"occupied_iou es un IoU en (0, 1], no {occupied_iou!r}")
        if to_occupied_s <= 0:
            raise ValueError(f"to_occupied_s son segundos > 0, no {to_occupied_s!r}")
        if to_free_s <= 0:
            raise ValueError(f"to_free_s son segundos > 0, no {to_free_s!r}")
        plazas = tuple(zones)
        if not plazas:
            raise ValueError("sin plazas no hay nada que medir")
        ids = [plaza.id for plaza in plazas]
        repetidos = sorted({i for i in ids if ids.count(i) > 1})
        if repetidos:
            raise ValueError(f"hay plazas con el mismo id: {repetidos}")

        self.name = name
        self.every_n_frames = int(every_n_frames)
        self.occupied_iou = float(occupied_iou)
        self.to_occupied_s = float(to_occupied_s)
        self.to_free_s = float(to_free_s)
        self.zones = plazas
        self._geometria: GeometriaPorFotograma[tuple[_PlazaEnPixeles, ...]] = GeometriaPorFotograma(
            self._a_pixeles, nombre=name
        )
        self._estados: dict[str, _EstadoDePlaza] = {plaza.id: _EstadoDePlaza() for plaza in plazas}

    @classmethod
    def from_config(cls, cfg: ParkingOccupancyConfig, **kwargs: Any) -> ParkingOccupancyAnalyzer:
        """Construye desde el bloque `analyzers` del `project.yaml`. Es el unico puente
        con la capa de configuracion, y donde `zones.json` se lee y se valida: un fichero
        mal formado falla aqui, al arrancar, y no al primer fotograma."""
        return cls(
            cargar_zonas(cfg.zones_file),
            name=cfg.effective_name,
            every_n_frames=cfg.every_n_frames,
            occupied_iou=cfg.occupied_iou,
            to_occupied_s=cfg.hysteresis.to_occupied_s,
            to_free_s=cfg.hysteresis.to_free_s,
            **kwargs,
        )

    # --- Lo que se puede consultar ------------------------------------------------------

    def estado_de(self, zone_id: str) -> Ocupacion | None:
        """El estado confirmado de una plaza, o `None` antes del primer `process()`.
        `KeyError` si no existe."""
        return self._estados[zone_id].confirmado

    def en_espera(self, zone_id: str) -> bool:
        """Si la plaza tiene una racha en contra de su estado confirmado, a medias."""
        return self._estados[zone_id].en_espera

    @property
    def resolucion(self) -> tuple[int, int] | None:
        """`(width, height)` con la que estan convertidas las plazas, o `None` antes del
        primer `process()`."""
        return self._geometria.resolucion

    def resumen(self) -> str:
        """El KPI, tal y como se pinta: `"N libres de M"`."""
        return f"{self._cuantas(Ocupacion.LIBRE)} libres de {len(self.zones)}"

    def _cuantas(self, estado: Ocupacion) -> int:
        return sum(1 for e in self._estados.values() if e.confirmado is estado)

    # --- process --------------------------------------------------------------------------

    def process(
        self, frame: Frame, tracks: list[Track], detections: list[Detection]
    ) -> AnalyzerOutput:
        """Asigna cada deteccion a su plaza, avanza la histeresis y emite lo que cambia.

        `tracks` se ignora a proposito (decision 1 del issue, arriba).
        """
        del tracks
        plazas = self._geometria.para(frame)
        con_coche = self._plazas_ocupadas_ahora(plazas, detections)
        eventos: list[Event] = []
        for plaza in plazas:
            zone_id = plaza.poligono.id
            cambio = self._estados[zone_id].observar(
                zone_id in con_coche,
                frame.timestamp,
                to_occupied_s=self.to_occupied_s,
                to_free_s=self.to_free_s,
            )
            if cambio is None:
                continue
            _, nuevo = cambio
            eventos.append(
                Event(
                    analyzer=self.name,
                    name=EVENTO_OCUPADA if nuevo is Ocupacion.OCUPADA else EVENTO_LIBERADA,
                    timestamp=frame.timestamp,
                    attributes={"zone": zone_id},
                )
            )
        return AnalyzerOutput(events=eventos, metrics=self._metricas(frame.timestamp))

    def _plazas_ocupadas_ahora(
        self, plazas: Sequence[_PlazaEnPixeles], detections: Sequence[Detection]
    ) -> set[str]:
        """Las plazas con al menos una deteccion asignada. Cada deteccion va **solo** a la
        plaza de mayor IoU, y solo si ese IoU llega a `occupied_iou`."""
        ocupadas: set[str] = set()
        for deteccion in detections:
            mejor_id: str | None = None
            mejor_iou = 0.0
            for plaza in plazas:
                iou = plaza.mascara.iou_con(deteccion.bbox)
                if iou > mejor_iou:
                    mejor_id, mejor_iou = plaza.poligono.id, iou
            if mejor_id is not None and mejor_iou >= self.occupied_iou:
                ocupadas.add(mejor_id)
        return ocupadas

    def _metricas(self, timestamp: datetime) -> list[Metric]:
        """Las tres globales y una por plaza, calculadas del mismo estado para que cuadren:
        se llama despues de observar todas las plazas, asi que ninguna esta sin estado."""
        por_plaza = {
            zone_id: 1.0 if estado.confirmado is Ocupacion.OCUPADA else 0.0
            for zone_id, estado in self._estados.items()
        }
        ocupadas = sum(por_plaza.values())
        globales = (
            (METRICA_LIBRES, float(len(por_plaza)) - ocupadas),
            (METRICA_OCUPADAS, ocupadas),
            (METRICA_TOTAL, float(len(por_plaza))),
        )
        return [
            Metric(analyzer=self.name, name=nombre, value=valor, timestamp=timestamp)
            for nombre, valor in globales
        ] + [
            Metric(
                analyzer=self.name,
                name=METRICA_PLAZA,
                value=valor,
                timestamp=timestamp,
                labels={"zone": zone_id},
            )
            for zone_id, valor in por_plaza.items()
        ]

    def _a_pixeles(self, width: int, height: int) -> tuple[_PlazaEnPixeles, ...]:
        plazas = []
        for zona in self.zones:
            poligono = zona.to_pixels(width, height)
            plazas.append(
                _PlazaEnPixeles(
                    poligono=poligono, mascara=rasterizar_poligono(poligono, width, height)
                )
            )
        return tuple(plazas)

    # --- annotate -------------------------------------------------------------------------

    def annotate(self, image: np.ndarray, tracks: list[Track]) -> np.ndarray:
        """Cada plaza como poligono relleno semitransparente del color de su estado —verde
        libre, rojo ocupada, ambar esperando la histeresis, gris "todavia no se" (solo antes
        del primer fotograma)— con su id, y el resumen "N libres de M" arriba a la izquierda.

        Devuelve un array nuevo; el que recibe queda intacto. Convierte las plazas con la
        forma de **esta** imagen y sin tocar la geometria de `process`: un preview
        reescalado no puede disparar el aviso de cambio de forma. Los tracks no se pintan:
        este analizador no los usa y dibujarlos sugeriria lo contrario.
        """
        del tracks
        lienzo = copia_para_anotar(image)
        alto, ancho = lienzo.shape[:2]
        for zona in self.zones:
            poligono = zona.to_pixels(ancho, alto)
            color = self._color_de(self._estados[zona.id])
            dibujar_poligono_relleno(lienzo, poligono.puntos, color=color)
            x1, y1, _, _ = poligono.rectangulo_envolvente
            dibujar_etiqueta(
                lienzo,
                zona.id,
                (x1 + 4, y1 + 4),
                color_fondo=color,
                color_texto=NEGRO if color is not ROJO else BLANCO,
                escala=0.5,
                grosor=1,
            )
        dibujar_etiqueta(lienzo, self.resumen(), (12, 12), color_fondo=BLANCO, color_texto=NEGRO)
        return lienzo

    @staticmethod
    def _color_de(estado: _EstadoDePlaza) -> Color:
        if estado.confirmado is None:
            return GRIS  # antes del primer fotograma: todavia no se
        if estado.en_espera:
            return AMBAR
        return ROJO if estado.confirmado is Ocupacion.OCUPADA else VERDE
