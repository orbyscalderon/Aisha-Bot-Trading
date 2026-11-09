"""Tests del montaje del control por Telegram desde la CLI."""

from __future__ import annotations

import httpx
import pytest

from cli import build_chat_listener, parse_chat_allowlist
from core.models import Mode
from tests.test_cli import write_yaml


@pytest.fixture
def cfg(tmp_path):
    from cli import _load
    return _load(write_yaml(tmp_path / "paper.yaml"))


@pytest.fixture(autouse=True)
def sin_entorno_heredado(monkeypatch):
    """Las variables reales de la máquina no deben decidir el resultado de un test."""
    for clave in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(clave, raising=False)


# --- la allowlist ---

@pytest.mark.parametrize("texto,esperado", [
    ("123", {"123"}),
    ("123,456", {"123", "456"}),
    ("123, 456 ; 789", {"123", "456", "789"}),
    ("  123  ", {"123"}),
    ("123,,456", {"123", "456"}),
])
def test_formas_de_escribir_la_allowlist(texto, esperado):
    assert parse_chat_allowlist(texto) == esperado


@pytest.mark.parametrize("texto", [None, "", "   ", ",", " ; , "])
def test_una_allowlist_vacia_no_autoriza_a_nadie(texto):
    """Falla cerrado. Si una cadena vacía significara 'todos', un .env a medio rellenar
    abriría el bot a cualquiera que acertase el nombre del bot en Telegram."""
    assert parse_chat_allowlist(texto) == frozenset()


# --- el montaje ---

def test_sin_token_no_hay_chat_pero_se_explica(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "123")
    listener, motivo = build_chat_listener(cfg, tmp_path)
    assert listener is None and "TELEGRAM_BOT_TOKEN" in motivo


def test_sin_allowlist_no_hay_chat_pero_se_explica(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    listener, motivo = build_chat_listener(cfg, tmp_path)
    assert listener is None and "nadie autorizado" in motivo


def test_con_token_y_allowlist_se_monta(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "123")
    listener, motivo = build_chat_listener(cfg, tmp_path)
    assert listener is not None and motivo == ""


def test_si_no_hay_allowlist_propia_se_usa_el_chat_de_las_alertas(cfg, tmp_path, monkeypatch):
    """Quien ya recibe alertas en un chat es, casi con seguridad, su dueño. Ahorra una clave
    más que rellenar sin abrir nada: sigue siendo una allowlist de uno."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    listener, _ = build_chat_listener(cfg, tmp_path)
    assert listener._dispatcher.authorized == frozenset({"555"})


def test_la_allowlist_propia_manda_sobre_la_de_alertas(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")
    listener, _ = build_chat_listener(cfg, tmp_path)
    assert listener._dispatcher.authorized == frozenset({"111", "222"})


# --- de punta a punta, sin red ---

def test_un_comando_real_recorre_todo_el_camino(cfg, tmp_path, monkeypatch):
    """Mensaje de Telegram -> intérprete -> dispatcher -> informes -> respuesta enviada."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "777")
    enviados = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            import json
            enviados.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 777}, "text": "/status"}},
        ]})

    listener, _ = build_chat_listener(
        cfg, tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    listener.poll_once()
    assert len(enviados) == 1
    assert "paper" in enviados[0]["text"]
    assert "posiciones abiertas: 0" in enviados[0]["text"]


def test_un_desconocido_no_recibe_respuesta_ni_siquiera_un_error(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "777")
    enviados = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            enviados.append(1)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 666}, "text": "/stop"}},
        ]})

    listener, _ = build_chat_listener(
        cfg, tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    listener.poll_once()
    assert enviados == []
    assert not (tmp_path / "STOP").exists()  # y no ejecutó nada


# --- el intérprete de lenguaje natural ---

def test_sin_clave_de_llm_solo_hay_reglas(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    listener, _ = build_chat_listener(cfg, tmp_path)
    assert [p.name for p in listener._dispatcher.parsers] == ["rules"]


def test_con_clave_de_llm_se_añade_claude_DETRAS_de_las_reglas(cfg, tmp_path, monkeypatch):
    """El orden importa: un comando exacto no debe depender de una llamada de red."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-x")
    listener, _ = build_chat_listener(cfg, tmp_path)
    assert [p.name for p in listener._dispatcher.parsers] == ["rules", "claude"]


def test_un_comando_exacto_no_llama_al_llm(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-x")
    llamadas = []

    def llm(_request):
        llamadas.append(1)
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"command":"STOP"}'}]})

    listener, _ = build_chat_listener(
        cfg, tmp_path, llm_client=httpx.Client(transport=httpx.MockTransport(llm))
    )
    listener._dispatcher.handle("1", "/status")
    assert llamadas == []


def test_una_frase_libre_si_llega_al_llm(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-x")

    def llm(_request):
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": '{"command":"PAUSE"}'}]})

    listener, _ = build_chat_listener(
        cfg, tmp_path, llm_client=httpx.Client(transport=httpx.MockTransport(llm))
    )
    respuesta = listener._dispatcher.handle("1", "oye para de comprar un rato")
    assert "pausa" in respuesta.lower()
    assert (tmp_path / "PAUSE").exists()


def test_si_el_llm_esta_caido_el_bot_sigue_respondiendo(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-x")

    def caido(_request):
        raise httpx.ConnectError("sin red")

    listener, _ = build_chat_listener(
        cfg, tmp_path, llm_client=httpx.Client(transport=httpx.MockTransport(caido))
    )
    assert "/status" in listener._dispatcher.handle("1", "oye para de comprar")   # la ayuda
    assert "paper" in listener._dispatcher.handle("1", "/status")                  # y lo exacto
