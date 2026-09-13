"""La agregacion por minuto, que es donde esta el riesgo de #9.

**La regla que manda este modulo: el tiempo lo dan los timestamps, nunca el numero de
fotogramas.** Desde #23 el connector en directo ajusta su tasa efectiva a lo que el
analisis sostiene —QA midio 5,0 -> 2,74 -> 5,0 fps dentro de una misma ejecucion—, y nada
la ancla a las fronteras de minuto: la tasa se mueve **dentro** del minuto, y se mueve con
sesgo, porque el analisis va mas lento justo cuando hay mas objetos en escena. Los minutos
con mas trafico son los que se muestrean menos.

Por eso la media de un minuto es **una media en el tiempo, no una media por muestra**:

    mean_value = integral / seconds

donde `integral` es la suma de valor x segundos que cada muestra representa y `seconds` el
tiempo cubierto. Una media por muestra (`sum_value / samples`) coincide con esta solo si las
muestras van uniformes dentro del minuto, y QA midio lo que pasa cuando no van: 4,9 % de
error si la maquina se carga a mitad de minuto, 24,2 % si la escena se llena y por eso cae
el fps, 78,6 % con 55 s parado a 5 fps y 5 s de pico a 1 fps. Y siempre hacia abajo: es el
dashboard aplanando las horas punta. `samples` y `sum_value` se guardan igualmente, pero
como cuenta y como suma, nunca como divisor de tiempo.

## Cuanto tiempo representa una muestra

Cada muestra cubre **desde el punto medio con la anterior hasta el punto medio con la
siguiente**, y ese tramo se recorta dos veces:

1. **A `HUECO_MAXIMO_S` por cada lado.** Una muestra no puede representar mas de eso: si
   la fuente se cae diez minutos, la ultima medida antes de la caida no vale para los diez
   minutos siguientes. Mas alla del tope no hay medida, hay un hueco, y un hueco se queda
   sin cubrir (`seconds` no crece) en vez de rellenarse con un valor viejo. La grafica
   ensena un agujero, que es lo que hubo.
2. **A su propio minuto.** Una muestra de las 14:20:59,9 no escribe tiempo en las 14:21.
   Cuando dos muestras consecutivas caen en minutos distintos, el corte entre ellas es la
   frontera en vez del punto medio: la de antes cubre hasta la frontera y la de despues
   empieza en ella. No se pierde ni un segundo, solo cambia de dueno el tramo entre el
   punto medio y la frontera. Lo que se gana es que un minuto tiene fila unicamente si
   tiene muestras, y que `min_value` y `max_value` siempre son de muestras que estan en la
   fila.

La primera muestra de una serie empieza en su propio timestamp: antes de ella no se estaba
mirando.

## El borde: la ultima muestra del lote

El tramo de una muestra no se conoce hasta que llega la siguiente, y la siguiente puede
venir en el lote de despues o no venir nunca. Las salidas que se descartaron:

- **Extenderla hasta la frontera del minuto.** Miente dos veces: al parar el worker a las
  14:20:10 diria que se midieron 60 s, y cuando llega la muestra siguiente a las 14:20:10,2
  ya se ha contado tiempo que es suyo. Deja de ser acumulable.
- **Cerrarla en su propio timestamp** (anchura cero, sin estado entre lotes). Pierde un
  tramo por lote, y con `batch_size: 1` —que el sink ofrece como "no se pierde nada"— lo
  pierde todo: ninguna muestra tendria tiempo.

Lo que se hace: **la ultima muestra de cada serie se queda pendiente en el agregador, y su
tramo se escribe en el lote en el que llega la siguiente.** Sus `samples`, `sum_value`,
`min_value` y `max_value` van en el lote en el que llego; sus `seconds` e `integral`, en el
siguiente. Es un objeto por serie en memoria, y la acumulacion sigue siendo exacta con
cualquier tamano de lote. Las consecuencias, para que nadie las descubra por su cuenta:

- La ultima muestra de una ejecucion no tiene tramo: no se sabe cuanto duro. Cuenta como
  muestra y no como tiempo.
- Un reinicio del worker pierde el tramo de esa muestra, y nada mas.
- Una muestra que llega **antes** que la pendiente (desorden entre lotes) cuenta como
  muestra y no como tiempo: reabrir un tramo ya escrito lo contaria dos veces.

`AgregadorPorMinuto` es ese estado. `agregar_por_minuto` es la version de un solo lote, para
quien no tiene estado que guardar: su ultima muestra por serie se queda sin tramo.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cvp.core import Metric
from cvp.storage.models import clave_de_labels

__all__ = [
    "HUECO_MAXIMO_S",
    "Agregado",
    "AgregadorPorMinuto",
    "ClaveMinuto",
    "ClaveSerie",
    "agregar_por_minuto",
    "minuto_de",
]

# Lo maximo que una muestra representa hacia cada lado, en segundos. El analizador mas
# lento del MVP es el de aparcamiento a ~0,5 fps (`every_n_frames: 10` sobre 5 fps), o sea
# una muestra cada 2 s; con la maquina cinco veces mas cargada de lo que midio QA serian
# 10 s entre muestras y 5 s de medio tramo. 15 s cubre eso con margen y sigue siendo mucho
# menos que un minuto: una caida de la fuente no rellena ni un minuto entero con un dato
# viejo.
HUECO_MAXIMO_S = 15.0

_UN_MINUTO = timedelta(minutes=1)


def minuto_de(marca: datetime) -> datetime:
    """El minuto UTC al que pertenece `marca`, truncado (no redondeado).

    En UTC siempre: con la hora local, la noche del cambio de hora tendria un minuto
    repetido y otro inexistente, y la serie del dashboard mentiria dos veces al ano.
    """
    return marca.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class ClaveSerie:
    """Una serie de medidas: el `(analyzer, name, labels)` de una metrica.

    Las etiquetas viajan ya como texto JSON ordenado (`clave_de_labels`) porque un mapping
    no es hashable y porque es exactamente lo que se guarda en la columna: la clave en
    memoria y la clave unica de la tabla son la misma cosa.
    """

    analyzer: str
    name: str
    labels: str

    @classmethod
    def de_metrica(cls, metrica: Metric) -> ClaveSerie:
        return cls(
            analyzer=metrica.analyzer,
            name=metrica.name,
            labels=clave_de_labels(dict(metrica.labels)),
        )


@dataclass(frozen=True, slots=True)
class ClaveMinuto:
    """A que celda de `metric_minute` pertenece una muestra: su serie y su minuto."""

    minute: datetime
    analyzer: str
    name: str
    labels: str

    @classmethod
    def de_metrica(cls, metrica: Metric) -> ClaveMinuto:
        return cls.de_serie(ClaveSerie.de_metrica(metrica), minuto_de(metrica.timestamp))

    @classmethod
    def de_serie(cls, serie: ClaveSerie, minute: datetime) -> ClaveMinuto:
        return cls(minute=minute, analyzer=serie.analyzer, name=serie.name, labels=serie.labels)


@dataclass(slots=True)
class Agregado:
    """Lo que se sabe de un minuto.

    Dos familias de campos, y cada una se acumula por su lado:

    - **De las muestras**: `samples`, `sum_value`, `min_value`, `max_value`. Entran cuando
      la muestra llega.
    - **Del tiempo**: `seconds` (tiempo cubierto dentro del minuto) e `integral` (suma de
      valor x segundos). Entran cuando se conoce el tramo de la muestra, que puede ser un
      lote mas tarde.

    La media es `integral / seconds`. Se guarda la integral y no la media porque es lo
    unico acumulable: promediar dos medias solo vale si los dos trozos cubren el mismo
    tiempo, y no lo cubren. `sum_value` sigue haciendo falta por el mismo motivo para la
    media por muestra, que es lo unico que hay mientras un minuto no tiene tiempo cubierto.
    """

    samples: int = 0
    sum_value: float = 0.0
    min_value: float = 0.0
    max_value: float = 0.0
    seconds: float = 0.0
    integral: float = 0.0

    @property
    def mean_value(self) -> float:
        """La media **en el tiempo** del minuto: `integral / seconds`.

        Mientras el minuto no tiene tiempo cubierto —una sola muestra cuyo tramo aun no se
        ha cerrado— la unica estimacion disponible es la de las muestras, y con una muestra
        es exactamente su valor. Es el unico caso en que se divide por `samples`, y no es
        una tasa: es un valor.
        """
        if self.seconds > 0:
            return self.integral / self.seconds
        return self.sum_value / self.samples if self.samples else 0.0

    def anadir_valor(self, valor: float) -> None:
        if self.samples == 0:
            self.min_value = valor
            self.max_value = valor
        else:
            self.min_value = min(self.min_value, valor)
            self.max_value = max(self.max_value, valor)
        self.samples += 1
        self.sum_value += valor

    def anadir_tiempo(self, valor: float, segundos: float) -> None:
        if segundos <= 0:
            return
        self.seconds += segundos
        self.integral += valor * segundos

    def unido_con(self, otro: Agregado) -> Agregado:
        """Los dos trozos del mismo minuto, como uno solo.

        Es lo que hace que reescribir un minuto **acumule** en vez de reemplazar: un worker
        que se reinicia a mitad de minuto no puede borrar lo que ya habia. Un trozo sin
        muestras (solo tiempo: el tramo de una muestra que llego en el lote anterior) no
        toca el minimo ni el maximo.
        """
        if self.samples == 0:
            minimo, maximo = otro.min_value, otro.max_value
        elif otro.samples == 0:
            minimo, maximo = self.min_value, self.max_value
        else:
            minimo = min(self.min_value, otro.min_value)
            maximo = max(self.max_value, otro.max_value)
        return Agregado(
            samples=self.samples + otro.samples,
            sum_value=self.sum_value + otro.sum_value,
            min_value=minimo,
            max_value=maximo,
            seconds=self.seconds + otro.seconds,
            integral=self.integral + otro.integral,
        )


@dataclass(slots=True)
class _Pendiente:
    """La ultima muestra de una serie: borde izquierdo ya decidido, derecho todavia no."""

    timestamp: datetime
    value: float
    desde: datetime


class AgregadorPorMinuto:
    """Agrupa metricas por (minuto, serie) y las resume, lote a lote.

    Guarda entre lotes la ultima muestra de cada serie, que es lo que hace que la
    acumulacion sea exacta con cualquier tamano de lote (ver el docstring del modulo).
    Un agregador por sink; un reinicio del worker estrena uno vacio.
    """

    def __init__(self, *, hueco_maximo_s: float = HUECO_MAXIMO_S) -> None:
        if hueco_maximo_s <= 0:
            raise ValueError(f"hueco_maximo_s son segundos > 0, no {hueco_maximo_s!r}")
        self._hueco = timedelta(seconds=hueco_maximo_s)
        self._pendientes: dict[ClaveSerie, _Pendiente] = {}

    @property
    def pendientes(self) -> int:
        """Cuantas series tienen una muestra esperando a que llegue la siguiente."""
        return len(self._pendientes)

    def agregar(self, metricas: Iterable[Metric]) -> Mapping[ClaveMinuto, Agregado]:
        """Lo que este lote **anade** a cada minuto. Se suma a lo que ya haya en la base.

        El orden de llegada dentro del lote da igual: se ordena por timestamp antes de
        decidir tramos. Y la composicion del lote tampoco: las mismas muestras repartidas
        en otros lotes dan, sumadas, el mismo resultado.
        """
        delta: dict[ClaveMinuto, Agregado] = defaultdict(Agregado)
        por_serie: dict[ClaveSerie, list[Metric]] = defaultdict(list)
        for metrica in metricas:
            por_serie[ClaveSerie.de_metrica(metrica)].append(metrica)

        for serie, muestras in por_serie.items():
            muestras.sort(key=lambda m: m.timestamp)
            for muestra in muestras:
                celda = ClaveMinuto.de_serie(serie, minuto_de(muestra.timestamp))
                delta[celda].anadir_valor(muestra.value)
                self._avanzar(delta, serie, muestra)
        return dict(delta)

    def _avanzar(
        self, delta: dict[ClaveMinuto, Agregado], serie: ClaveSerie, muestra: Metric
    ) -> None:
        """Cierra el tramo de la muestra pendiente y deja esta como pendiente."""
        pendiente = self._pendientes.get(serie)
        if pendiente is None:
            self._pendientes[serie] = _Pendiente(
                muestra.timestamp, muestra.value, desde=muestra.timestamp
            )
            return
        if muestra.timestamp <= pendiente.timestamp:
            # Llego tarde, o repetida: reabrir un tramo ya escrito lo contaria dos veces.
            return
        # El corte entre las dos es el punto medio, salvo que esten en minutos distintos:
        # entonces es la frontera, para que ninguna escriba tiempo fuera de su minuto y el
        # tramo entre el punto medio y la frontera no se quede sin dueno.
        frontera = minuto_de(muestra.timestamp)
        if frontera > pendiente.timestamp:
            corte = frontera
        else:
            corte = pendiente.timestamp + (muestra.timestamp - pendiente.timestamp) / 2
        self._integrar(delta, serie, pendiente, hasta=min(corte, pendiente.timestamp + self._hueco))
        self._pendientes[serie] = _Pendiente(
            muestra.timestamp, muestra.value, desde=max(corte, muestra.timestamp - self._hueco)
        )

    @staticmethod
    def _integrar(
        delta: dict[ClaveMinuto, Agregado],
        serie: ClaveSerie,
        pendiente: _Pendiente,
        *,
        hasta: datetime,
    ) -> None:
        """Escribe el tramo `[desde, hasta)` de la muestra, recortado a su propio minuto."""
        minuto = minuto_de(pendiente.timestamp)
        desde = max(pendiente.desde, minuto)
        hasta = min(hasta, minuto + _UN_MINUTO)
        if hasta > desde:
            delta[ClaveMinuto.de_serie(serie, minuto)].anadir_tiempo(
                pendiente.value, (hasta - desde).total_seconds()
            )


def agregar_por_minuto(metricas: Iterable[Metric]) -> Mapping[ClaveMinuto, Agregado]:
    """Un solo lote, sin estado: la ultima muestra de cada serie se queda sin tramo.

    Vale para un test o para una escritura suelta. El sink usa un `AgregadorPorMinuto`
    propio, porque entre sus lotes si hay una muestra siguiente.
    """
    return AgregadorPorMinuto().agregar(metricas)
