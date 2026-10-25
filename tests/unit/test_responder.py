"""El responder de guardia (`ops/responder/responder.py`), con `claude` y `docker` falsos.

Nada sale a la red ni lanza procesos: el responder recibe su `run`, su `http_get`, su reloj y
su `sleep` por inyeccion.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

RUTA = Path(__file__).resolve().parents[2] / "ops" / "responder" / "responder.py"
_spec = importlib.util.spec_from_file_location("responder", RUTA)
assert _spec and _spec.loader
responder = importlib.util.module_from_spec(_spec)
sys.modules["responder"] = responder
_spec.loader.exec_module(responder)

T0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
ALERTA = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "DataStale", "service": "worker", "project": "00-demo"},
        }
    ],
}


class Reloj:
    def __init__(self) -> None:
        self.ahora = T0

    def __call__(self) -> datetime:
        return self.ahora

    def dormir(self, segundos: float) -> None:
        self.ahora += timedelta(seconds=segundos)


class Mundo:
    """`docker` y `claude` falsos: registra cada comando y contesta con lo configurado."""

    def __init__(self, reloj: Reloj, respuesta_agente: str, se_recupera: bool = True) -> None:
        self.reloj = reloj
        self.respuesta_agente = respuesta_agente
        self.se_recupera = se_recupera
        self.comandos: list[list[str]] = []
        self.entrada_agente = ""
        self.reiniciado = False

    def run(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
        self.comandos.append(cmd)
        assert kw.get("shell") is False
        if cmd[0] == "docker" and "restart" in cmd:
            self.reiniciado = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "docker" and "ps" in cmd:
            fila = {"Service": "worker", "State": "exited", "Status": "Exited (1)", "Env": "SECRET"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(fila) + "\n", "")
        if cmd[0] == "docker" and "logs" in cmd:
            logs = "worker | arranca\nworker | API_KEY=abc123\nworker | DATABASE_URL=x\n"
            logs += "worker | url postgresql://cvp:pw@db/cvp\nworker | HOOK=https://h\n"
            logs += "worker | muestra normal\n"
            return subprocess.CompletedProcess(cmd, 0, logs, "")
        self.entrada_agente = kw.get("input", "")
        return subprocess.CompletedProcess(cmd, 0, self.respuesta_agente, "")

    def http_get(self, url: str) -> dict[str, Any]:
        sano = self.reiniciado and self.se_recupera
        edad = 30 if sano else 600
        minuto = (self.reloj() - timedelta(seconds=edad)).isoformat()
        return {"project_id": "00-demo", "latest": [{"minute": minuto, "samples": 3}]}

    def comandos_docker(self) -> list[list[str]]:
        return [c for c in self.comandos if c[0] == "docker"]


def montar(tmp_path: Path, respuesta: str, se_recupera: bool = True):
    reloj = Reloj()
    mundo = Mundo(reloj, respuesta, se_recupera)
    resp = responder.Responder(
        run=mundo.run,
        http_get=mundo.http_get,
        clock=reloj,
        sleep=reloj.dormir,
        incidents_dir=tmp_path,
        claude_bin="claude-falso",
        emit=lambda _linea: None,
    )
    return resp, mundo, reloj


REINICIAR = json.dumps({"diagnosis": "worker caido", "action": "restart_worker", "reason": "x"})


def eventos(incidente: dict[str, Any]) -> list[str]:
    return [e["event"] for e in incidente["timeline"]]


def test_json_valido_reinicia_y_verifica(tmp_path: Path) -> None:
    resp, mundo, _ = montar(tmp_path, REINICIAR)
    incidente = resp.handle_incident("00-demo")

    assert incidente["outcome"] == "recovered"
    assert eventos(incidente) == [
        "alert_received",
        "evidence_collected",
        "agent_decision",
        "action_executed",
        "verification",
    ]
    reinicios = [c for c in mundo.comandos_docker() if "restart" in c]
    assert len(reinicios) == 1
    assert reinicios[0][:2] == ["docker", "compose"] and reinicios[0][-2:] == ["restart", "worker"]
    assert list(tmp_path.glob("*-incident.json")) and list(tmp_path.glob("*-evidence.json"))


def test_el_agente_se_invoca_sin_herramientas(tmp_path: Path) -> None:
    resp, mundo, _ = montar(tmp_path, REINICIAR)
    resp.handle_incident("00-demo")
    cmd = next(c for c in mundo.comandos if c[0] == "claude-falso")
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--model" in cmd and "-p" in cmd


@pytest.mark.parametrize(
    "salida",
    [
        "no es json",
        "[]",
        json.dumps({"diagnosis": "d", "action": "restart_worker"}),
        json.dumps({"diagnosis": "d", "action": "restart_worker", "reason": "r", "x": "y"}),
        json.dumps({"diagnosis": "d", "action": "restart_worker", "reason": 3}),
        json.dumps({"diagnosis": "d", "action": "rm -rf /", "reason": "r"}),
        json.dumps({"diagnosis": "d", "action": "restart_api", "reason": "r"}),
        json.dumps({"diagnosis": "d", "action": "docker compose down", "reason": "r"}),
    ],
)
def test_salida_invalida_o_fuera_de_allowlist_no_ejecuta_nada(tmp_path: Path, salida: str) -> None:
    resp, mundo, _ = montar(tmp_path, salida)
    incidente = resp.handle_incident("00-demo")

    assert incidente["outcome"] == "no_action"
    decision = next(e for e in incidente["timeline"] if e["event"] == "agent_decision")
    assert decision["decision"]["action"] == "none"
    assert decision["validation_error"]
    assert not [c for c in mundo.comandos_docker() if "restart" in c]


def test_tolera_un_bloque_de_codigo_y_nada_mas() -> None:
    decision, error = responder.parse_decision(f"```json\n{REINICIAR}\n```")
    assert error is None and decision["action"] == "restart_worker"
    _, error = responder.parse_decision(f"Claro: {REINICIAR}")
    assert error


def test_si_el_agente_falla_la_accion_es_none(tmp_path: Path) -> None:
    resp, mundo, _ = montar(tmp_path, "")
    run_original = mundo.run

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if cmd[0] == "claude-falso":
            raise subprocess.TimeoutExpired(cmd, 1)
        return run_original(cmd, **kw)

    resp.run = run
    assert resp.handle_incident("00-demo")["outcome"] == "no_action"


def test_rate_limit_un_reinicio_cada_10_minutos(tmp_path: Path) -> None:
    resp, mundo, reloj = montar(tmp_path, REINICIAR)
    assert resp.handle_incident("00-demo")["outcome"] == "recovered"

    mundo.reiniciado = False  # el worker vuelve a caerse
    reloj.ahora += timedelta(minutes=2)
    segundo = resp.handle_incident("00-demo")
    assert segundo["outcome"] == "escalated"
    assert "action_skipped" in eventos(segundo)
    assert len([c for c in mundo.comandos_docker() if "restart" in c]) == 1

    reloj.ahora += timedelta(minutes=10)
    assert resp.handle_incident("00-demo")["outcome"] == "recovered"
    assert len([c for c in mundo.comandos_docker() if "restart" in c]) == 2


def test_si_tras_reiniciar_persiste_escala(tmp_path: Path) -> None:
    resp, mundo, _ = montar(tmp_path, REINICIAR, se_recupera=False)
    incidente = resp.handle_incident("00-demo")
    assert incidente["outcome"] == "not_recovered"
    assert eventos(incidente)[-1] == "escalated"
    assert len([c for c in mundo.comandos_docker() if "restart" in c]) == 1


def test_la_evidencia_no_lleva_secretos(tmp_path: Path) -> None:
    resp, mundo, _ = montar(tmp_path, REINICIAR)
    resp.handle_incident("00-demo")

    ficheros = list(tmp_path.glob("*-evidence.json"))
    texto = ficheros[0].read_text(encoding="utf-8") + mundo.entrada_agente
    for secreto in ("abc123", "API_KEY", "DATABASE_URL", "postgresql://", "HOOK", "SECRET"):
        assert secreto not in texto
    assert "muestra normal" in texto
    evidencia = json.loads(ficheros[0].read_text(encoding="utf-8"))
    assert evidencia["metrics"]["age_seconds"] == 600


def test_redact_recorta_a_los_ultimos_bytes() -> None:
    texto = "\n".join(f"linea {i}" for i in range(5000))
    salida = responder.redact(texto, max_bytes=100)
    assert len(salida.encode()) <= 100 and salida.endswith("linea 4999")


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "resolved", "alerts": ALERTA["alerts"]},
        {"status": "firing", "alerts": [{**ALERTA["alerts"][0], "status": "resolved"}]},
        {"status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "Otra"}}]},
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "DataStale", "service": "api", "project": "p"},
                }
            ],
        },
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "DataStale", "service": "worker", "project": "../x"},
                }
            ],
        },
        {"status": "firing"},
        [],
        "texto",
        None,
    ],
)
def test_ignora_lo_que_no_es_datastale_del_worker(tmp_path: Path, payload: Any) -> None:
    resp, mundo, _ = montar(tmp_path, REINICIAR)
    assert resp.handle_payload(payload, background=False) == "ignored"
    assert mundo.comandos == []
    assert not list(tmp_path.iterdir())


def test_atiende_la_alerta_datastale_firing(tmp_path: Path) -> None:
    resp, mundo, _ = montar(tmp_path, REINICIAR)
    assert resp.handle_payload(ALERTA, background=False) == "accepted"
    assert mundo.reiniciado
    # El cerrojo se libera: una segunda alerta se vuelve a atender.
    assert resp.handle_payload(ALERTA, background=False) == "accepted"
