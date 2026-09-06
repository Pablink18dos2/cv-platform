"""La agregacion por minuto, que es donde esta el riesgo de #9.

Una funcion pura y dos tipos: se prueba sin base de datos, sin fotogramas y sin reloj, que
es lo que permite escribir su test antes que ella.

**La regla que manda este modulo: el tiempo lo dan los timestamps, nunca el numero de
fotogramas.** Desde #23 el connector en directo ajusta su tasa efectiva a lo que el
analisis sostiene —QA midio 5,0 -> 2,74 -> 5,0 fps dentro de una misma ejecucion, segun se
cargaba y descargaba la maquina—, y eso es deliberado: es lo que evita que los fotogramas
se descarten en bloque y aparezcan saltos de mas de un segundo entre marcas. La
consecuencia directa es que **el numero de muestras de un minuto no es constante y no se
puede suponer**.

Por eso aqui no hay ninguna division por muestras que represente tiempo. Un agregado de la
forma "cuentas del minuto x target_fps / muestras del minuto" saldria bien en un portatil
que va sobrado y mal en cuanto la maquina se cargue, que es la peor clase de fallo: el que
solo aparece en produccion y de noche. Lo unico que se divide por `samples` es la media de
un valor, que es una media de valores y no una tasa por unidad de tiempo.

Y el minuto de una muestra sale de **su propio timestamp**, truncado a minuto en UTC:

- no del momento en que se escribe, que llega despues y en lotes;
- no del minuto del lote, que es lo que hace que un lote a caballo entre dos minutos se
  vaya entero al minuto equivocado.

Cada muestra se coloca sola en su minuto, asi que un lote que cruza una frontera se reparte
entre los dos sin que nadie tenga que darse cuenta de que la cruzo.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from cvp.core import Metric
from cvp.storage.models import clave_de_labels

__all__ = ["Agregado", "ClaveMinuto", "agregar_por_minuto", "minuto_de"]


def minuto_de(marca: datetime) -> datetime:
    """El minuto UTC al que pertenece `marca`, truncado (no redondeado).

    En UTC siempre: con la hora local, la noche del cambio de hora tendria un minuto
    repetido y otro inexistente, y la serie del dashboard mentiria dos veces al ano.
    """
    return marca.astimezone(UTC).replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class ClaveMinuto:
    """A que celda de `metric_minute` pertenece una muestra.

    Las etiquetas viajan ya como texto JSON ordenado (`clave_de_labels`) porque un mapping
    no es hashable y porque es exactamente lo que se guarda en la columna: la clave del
    diccionario en memoria y la clave unica de la tabla son la misma cosa.
    """

    minute: datetime
    analyzer: str
    name: str
    labels: str

    @classmethod
    def de_metrica(cls, metrica: Metric) -> ClaveMinuto:
        return cls(
            minute=minuto_de(metrica.timestamp),
            analyzer=metrica.analyzer,
            name=metrica.name,
            labels=clave_de_labels(dict(metrica.labels)),
        )


@dataclass(slots=True)
class Agregado:
    """Lo que se sabe de un minuto: cuantas muestras, su suma, su minimo y su maximo.

    Se guarda la **suma** y no solo la media porque es lo unico que se puede acumular: para
    unir dos trozos del mismo minuto hay que sumar sumas y sumar cuentas, mientras que
    promediar dos medias solo es correcto si los dos trozos tienen el mismo numero de
    muestras, y desde #23 justamente no lo tienen.
    """

    samples: int = 0
    sum_value: float = 0.0
    min_value: float = 0.0
    max_value: float = 0.0

    @classmethod
    def de_valor(cls, valor: float) -> Agregado:
        return cls(samples=1, sum_value=valor, min_value=valor, max_value=valor)

    @property
    def mean_value(self) -> float:
        """La media de los valores del minuto. Divide por muestras porque es una media de
        valores; ninguna tasa por unidad de tiempo se calcula asi."""
        return self.sum_value / self.samples if self.samples else 0.0

    def anadir(self, valor: float) -> None:
        if self.samples == 0:
            self.min_value = valor
            self.max_value = valor
        else:
            self.min_value = min(self.min_value, valor)
            self.max_value = max(self.max_value, valor)
        self.samples += 1
        self.sum_value += valor

    def unido_con(self, otro: Agregado) -> Agregado:
        """Los dos trozos del mismo minuto, como uno solo.

        Es lo que hace que reescribir un minuto **acumule** en vez de reemplazar: un worker
        que se reinicia a mitad de minuto no puede borrar lo que ya habia.
        """
        if self.samples == 0:
            return Agregado(otro.samples, otro.sum_value, otro.min_value, otro.max_value)
        if otro.samples == 0:
            return Agregado(self.samples, self.sum_value, self.min_value, self.max_value)
        return Agregado(
            samples=self.samples + otro.samples,
            sum_value=self.sum_value + otro.sum_value,
            min_value=min(self.min_value, otro.min_value),
            max_value=max(self.max_value, otro.max_value),
        )


def agregar_por_minuto(metricas: Iterable[Metric]) -> Mapping[ClaveMinuto, Agregado]:
    """Agrupa las metricas por (minuto, analyzer, name, labels) y las resume.

    El orden de llegada da igual y la composicion del lote tambien: dos lotes cualesquiera
    de las mismas muestras dan el mismo resultado, porque cada muestra se coloca en el
    minuto de su propio timestamp. Es lo que hace que un lote a caballo entre dos minutos
    se reparta solo.
    """
    agregados: dict[ClaveMinuto, Agregado] = {}
    for metrica in metricas:
        clave = ClaveMinuto.de_metrica(metrica)
        agregado = agregados.get(clave)
        if agregado is None:
            agregados[clave] = Agregado.de_valor(metrica.value)
        else:
            agregado.anadir(metrica.value)
    return agregados
