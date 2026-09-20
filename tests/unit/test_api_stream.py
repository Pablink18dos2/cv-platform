"""`GET /projects/{id}/stream.mjpg`: MJPEG que solo emite cuando el fichero cambia.

Dos niveles. El generador `fotogramas_nuevos` se prueba solo, con una espera falsa: es
donde vive la regla de "solo si ha cambiado" y la de no dejar el fichero abierto. Y el
endpoint entero se prueba **con uvicorn de verdad** en un hilo, porque un
`multipart/x-mixed-replace` no termina nunca y el `TestClient` de Starlette espera a que
la respuesta acabe: hace falta un cliente que lea tres fotogramas y cuelgue, y un
servidor que se entere de que ha colgado.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_soporte import PROYECTO, Base, VigilanteDeFicheros
from fastapi.testclient import TestClient

from cvp.api import crear_app
from cvp.api.preview import fotogramas_nuevos
from cvp.api.routers.stream import FRONTERA

URL = f"/api/v1/projects/{PROYECTO}/stream.mjpg"


# --- El generador ---------------------------------------------------------------------------


# Con una espera que no duerme, un generador que no encuentra cambios gira sin ceder el
# control: el tope convierte ese bucle en un fallo en milisegundos y no en una suite colgada.
MAX_VUELTAS = 200


class EsperaFalsa:
    """Sustituye a `asyncio.sleep`: no espera, cuenta, y ejecuta lo que el test programe
    entre vueltas (escribir el fichero, por ejemplo)."""

    def __init__(self) -> None:
        self.vueltas = 0
        self.entre_vueltas: dict[int, Any] = {}

    async def __call__(self, _segundos: float) -> None:
        self.vueltas += 1
        if self.vueltas > MAX_VUELTAS:
            raise AssertionError(
                f"el generador ha dado {MAX_VUELTAS} vueltas sin emitir lo esperado: bucle"
            )
        accion = self.entre_vueltas.get(self.vueltas)
        if accion is not None:
            accion()


def _tres(ruta: Path, espera: EsperaFalsa) -> list[bytes]:
    async def consumir() -> list[bytes]:
        generador = fotogramas_nuevos(ruta, intervalo_s=0.0, dormir=espera)
        salida: list[bytes] = []
        try:
            async for datos in generador:
                salida.append(datos)
                if len(salida) == 3:
                    break
        finally:
            await generador.aclose()
        return salida

    return asyncio.run(consumir())


def _escribir(ruta: Path, contenido: bytes, mtime_ns: int) -> None:
    """Escribe y fija el `mtime`: dos escrituras en el mismo tick de reloj tendrian el
    mismo, y lo que se prueba es que el generador mira el `mtime`, no el reloj."""
    ruta.write_bytes(contenido)
    os.utime(ruta, ns=(mtime_ns, mtime_ns))


def test_emite_solo_cuando_el_fichero_cambia(tmp_path: Path) -> None:
    """Tres versiones del fichero, con muchas vueltas sin cambios entre medias: salen
    exactamente tres fotogramas, y entre el primero y el segundo hubo vueltas en las que
    no se emitio nada aunque el fichero seguia ahi."""
    ruta = tmp_path / "p.jpg"
    _escribir(ruta, b"uno", 1_000)
    espera = EsperaFalsa()
    espera.entre_vueltas[5] = lambda: _escribir(ruta, b"dos", 2_000)
    espera.entre_vueltas[9] = lambda: _escribir(ruta, b"tres", 3_000)

    assert _tres(ruta, espera) == [b"uno", b"dos", b"tres"]
    assert espera.vueltas == 9


def test_el_mismo_contenido_con_otro_mtime_se_emite_y_el_mismo_mtime_no(tmp_path: Path) -> None:
    """La version es `(mtime, tamano)`: es lo que cambia con cada `os.replace` del sink,
    y es mas barato que comparar bytes. Un fichero reescrito igual pero con otro mtime
    es "nuevo"; uno con el mismo mtime y tamano no se vuelve a leer."""
    ruta = tmp_path / "p.jpg"
    _escribir(ruta, b"igual", 1_000)
    espera = EsperaFalsa()
    espera.entre_vueltas[1] = lambda: _escribir(ruta, b"igual", 1_000)  # nada cambia
    espera.entre_vueltas[2] = lambda: _escribir(ruta, b"igual", 2_000)  # cambia el mtime
    espera.entre_vueltas[3] = lambda: _escribir(ruta, b"distinto", 2_000)  # cambia el tamano

    assert _tres(ruta, espera) == [b"igual", b"igual", b"distinto"]
    assert espera.vueltas == 3


def test_un_fichero_con_el_mismo_mtime_y_tamano_no_se_detecta_y_el_test_no_se_cuelga(
    tmp_path: Path,
) -> None:
    """El limite de la version `(mtime, tamano)`, escrito: dos JPEG distintos del mismo
    tamano y en el mismo tick de reloj se ven como uno. Con `os.replace` y un `mtime` en
    nanosegundos no pasa en la practica; aqui se fuerza para dejar constancia, y para
    comprobar que la espera falsa corta el bucle en vez de colgar la suite."""
    ruta = tmp_path / "p.jpg"
    _escribir(ruta, b"igual", 1_000)
    espera = EsperaFalsa()
    espera.entre_vueltas[1] = lambda: _escribir(ruta, b"otro!", 1_000)
    with pytest.raises(AssertionError, match="bucle"):
        _tres(ruta, espera)


def test_si_el_fichero_desaparece_espera_y_sigue_cuando_vuelve(tmp_path: Path) -> None:
    ruta = tmp_path / "p.jpg"
    _escribir(ruta, b"uno", 1_000)
    espera = EsperaFalsa()
    espera.entre_vueltas[1] = ruta.unlink
    espera.entre_vueltas[4] = lambda: _escribir(ruta, b"dos", 2_000)
    espera.entre_vueltas[5] = lambda: _escribir(ruta, b"tres", 3_000)

    assert _tres(ruta, espera) == [b"uno", b"dos", b"tres"]


def test_entre_lecturas_no_queda_ningun_descriptor_abierto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = tmp_path / "p.jpg"
    _escribir(ruta, b"uno", 1_000)
    vigilante = VigilanteDeFicheros(monkeypatch, ruta)
    espera = EsperaFalsa()

    def comprobar_y_escribir(contenido: bytes, mtime: int) -> None:
        assert vigilante.sin_cerrar == []
        _escribir(ruta, contenido, mtime)

    espera.entre_vueltas[1] = lambda: comprobar_y_escribir(b"dos", 2_000)
    espera.entre_vueltas[2] = lambda: comprobar_y_escribir(b"tres", 3_000)

    assert _tres(ruta, espera) == [b"uno", b"dos", b"tres"]
    # Tres lecturas (las escrituras del propio test tambien pasan por `open`) y ninguna
    # sigue abierta: tampoco entre una emision y la siguiente, que es donde el MJPEG vive.
    assert sum(1 for f in vigilante.abiertos if "r" in f.mode) == 3
    assert vigilante.sin_cerrar == []


# --- El endpoint ----------------------------------------------------------------------------


def test_sin_fichero_es_404_snapshot_not_found(base: Base, cliente: TestClient) -> None:
    respuesta = cliente.get(URL)
    assert respuesta.status_code == 404
    assert respuesta.json()["error"]["code"] == "snapshot_not_found"


def test_un_id_sin_forma_de_proyecto_es_404(base: Base, cliente: TestClient) -> None:
    respuesta = cliente.get("/api/v1/projects/%2e%2e/stream.mjpg")
    assert respuesta.status_code == 404


class ServidorDePrueba:
    """uvicorn en un hilo, en un puerto libre, con la app que se le da."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        with socket.socket() as sonda:
            sonda.bind(("127.0.0.1", 0))
            self.puerto = sonda.getsockname()[1]
        self.servidor = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.puerto, log_level="warning")
        )
        self.hilo = threading.Thread(target=self.servidor.run, daemon=True)

    def __enter__(self) -> ServidorDePrueba:
        self.hilo.start()
        limite = time.monotonic() + 10
        while not self.servidor.started:
            if time.monotonic() > limite or not self.hilo.is_alive():
                raise RuntimeError("uvicorn no ha arrancado")
            time.sleep(0.01)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.servidor.should_exit = True
        self.hilo.join(timeout=10)
        assert not self.hilo.is_alive(), "uvicorn no ha parado: el stream ha dejado algo colgado"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.puerto}"


def _partes(respuesta: httpx.Response, cuantas: int) -> Iterator[bytes]:
    """Los cuerpos de las primeras `cuantas` partes del multipart, y cuelga."""
    separador = f"--{FRONTERA}\r\n".encode()
    buffer = b""
    emitidas = 0
    for trozo in respuesta.iter_bytes():
        buffer += trozo
        while emitidas < cuantas:
            inicio = buffer.find(separador)
            if inicio < 0:
                break
            fin_cabecera = buffer.find(b"\r\n\r\n", inicio)
            if fin_cabecera < 0:
                break
            cabecera = buffer[inicio + len(separador) : fin_cabecera].decode("ascii")
            longitud = int(cabecera.split("Content-Length: ")[1].split("\r\n")[0])
            assert "Content-Type: image/jpeg" in cabecera
            cuerpo_desde = fin_cabecera + 4
            if len(buffer) < cuerpo_desde + longitud + 2:
                break
            yield buffer[cuerpo_desde : cuerpo_desde + longitud]
            emitidas += 1
            buffer = buffer[cuerpo_desde + longitud + 2 :]
        if emitidas >= cuantas:
            return


def test_consume_tres_fotogramas_por_http_y_para(base: Base) -> None:
    """Con uvicorn de verdad: el cliente lee tres fotogramas distintos, cuelga, y el
    servidor se apaga limpio. Entre fotograma y fotograma el test escribe el fichero con
    el `PreviewSink` real, que es lo que en produccion hace el worker."""
    base.preview(color=10, indice=0)
    app = crear_app(base.settings(stream_interval_s=0.02), clock=base.reloj)
    with ServidorDePrueba(app) as servidor, httpx.Client(timeout=10) as cliente:
        with cliente.stream("GET", servidor.url + URL) as respuesta:
            assert respuesta.status_code == 200
            assert respuesta.headers["content-type"] == (
                f"multipart/x-mixed-replace; boundary={FRONTERA}"
            )
            assert respuesta.headers["cache-control"] == "no-store"
            partes = _partes(respuesta, 3)
            primero = next(partes)
            base.preview(color=120, indice=1)
            segundo = next(partes)
            base.preview(color=230, indice=2)
            tercero = next(partes)
        assert primero != segundo != tercero
        assert all(p[:3] == b"\xff\xd8\xff" for p in (primero, segundo, tercero))
        assert tercero == base.preview_dir.joinpath(f"{PROYECTO}.jpg").read_bytes()


def test_no_manda_el_mismo_fotograma_varias_veces(base: Base) -> None:
    """Con el fichero quieto durante muchas vueltas del intervalo, solo llega un
    fotograma: lo que se comprueba es que el segundo no aparece hasta que se escribe."""
    base.preview(color=10, indice=0)
    app = crear_app(base.settings(stream_interval_s=0.01), clock=base.reloj)
    with ServidorDePrueba(app) as servidor, httpx.Client(timeout=10) as cliente:
        with cliente.stream("GET", servidor.url + URL) as respuesta:
            partes = _partes(respuesta, 2)
            primero = next(partes)
            time.sleep(0.2)  # 20 vueltas sin cambios: si mandara copias, ya estarian aqui
            base.preview(color=200, indice=1)
            segundo = next(partes)
        assert segundo != primero
        assert segundo == base.preview_dir.joinpath(f"{PROYECTO}.jpg").read_bytes()
