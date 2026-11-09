"""Tests de `telegram-chat-id`, el paso que atasca a todo el mundo al montar un bot.

El token identifica al BOT; para que el bot te hable hace falta el número de TU conversación
con él, y Telegram solo lo revela después de que le escribas.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli import CliDeps, app
from tests.test_cli import write_yaml

TOKEN = "123:TOKEN-SECRETO-QUE-NO-DEBE-SALIR"


@pytest.fixture
def cfg_path(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def telegram(updates=(), me_status=200, updates_status=200, error=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if error:
            raise error
        if request.url.path.endswith("/getMe"):
            if me_status != 200:
                return httpx.Response(me_status, text="no")
            return httpx.Response(200, json={"ok": True, "result": {"username": "aisha_bot"}})
        if updates_status != 200:
            return httpx.Response(updates_status, text="no")
        return httpx.Response(200, json={"ok": True, "result": list(updates)})
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(cfg_path, client, monkeypatch, token=TOKEN):
    if token is not None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    # --root apunta al tmp_path, NO al proyecto: si no, el comando carga el .env real de quien
    # ejecuta la suite y un test de "falta el token" pasa o falla segun la maquina.
    return CliRunner().invoke(
        app, ["telegram-chat-id", "--config", str(cfg_path), "--root", str(cfg_path.parent)],
        obj=CliDeps(telegram_client=client),
    )


def msg(chat_id, tipo="private", quien="orbys"):
    return {"update_id": chat_id,
            "message": {"chat": {"id": chat_id, "type": tipo, "username": quien}, "text": "hola"}}


# --- el camino normal ---

def test_muestra_el_chat_id_de_quien_ha_escrito(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([msg(987654321)]), monkeypatch)
    assert r.exit_code == 0, r.output
    assert "987654321" in r.output
    assert "TELEGRAM_ALLOWED_CHAT_IDS=987654321" in r.output


def test_confirma_que_el_token_vale_antes_de_nada(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([msg(1)]), monkeypatch)
    assert "@aisha_bot" in r.output and "valido" in r.output


def test_varios_chats_se_listan_y_se_proponen_todos(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([msg(111), msg(222, quien="otro")]), monkeypatch)
    assert r.exit_code == 0, r.output
    assert "111" in r.output and "222" in r.output
    assert "TELEGRAM_ALLOWED_CHAT_IDS=111,222" in r.output


def test_un_chat_repetido_se_propone_una_sola_vez(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([msg(555), msg(555)]), monkeypatch)
    assert "TELEGRAM_ALLOWED_CHAT_IDS=555\n" in r.output


# --- lo que falta ---

def test_sin_mensajes_dice_exactamente_que_hacer(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([]), monkeypatch)
    assert r.exit_code == 1
    assert "Start" in r.output and "@aisha_bot" in r.output


def test_sin_token_lo_dice(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([]), monkeypatch, token=None)
    assert r.exit_code == 2 and "TELEGRAM_BOT_TOKEN" in r.output


def test_un_token_invalido_se_distingue_de_un_fallo_cualquiera(cfg_path, monkeypatch):
    r = run(cfg_path, telegram(me_status=401), monkeypatch)
    assert r.exit_code == 2 and "rechazo el token" in r.output


def test_un_fallo_de_red_no_suelta_una_traza(cfg_path, monkeypatch):
    r = run(cfg_path, telegram(error=httpx.ConnectError("sin red")), monkeypatch)
    assert r.exit_code == 2 and "ConnectError" in r.output


# --- el token no se filtra ---

def test_el_token_no_aparece_en_la_salida_ni_al_fallar(cfg_path, monkeypatch):
    r = run(cfg_path, telegram(error=httpx.ConnectError(f"caido en bot{TOKEN}/getMe")), monkeypatch)
    assert TOKEN not in r.output and "SECRETO" not in r.output


def test_el_token_tampoco_aparece_cuando_todo_va_bien(cfg_path, monkeypatch):
    r = run(cfg_path, telegram([msg(42)]), monkeypatch)
    assert TOKEN not in r.output


# --- no cambia nada ---

def test_no_toca_el_fichero_env(cfg_path, tmp_path, monkeypatch):
    """Solo informa: escribir en .env por su cuenta podria destrozar un fichero con secretos."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    run(cfg_path, telegram([msg(7)]), monkeypatch)
    assert env.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=x\n"
