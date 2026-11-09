"""Tests de que el CLI lee de verdad el `.env`, y no solo para HELIUS_API_KEY.

EL FALLO QUE FIJAN. Durante un tiempo `telegram-chat-id`, el notificador y el control por chat
leian sus claves SOLO de `os.environ`. Desde una terminal donde alguien las habia exportado a
mano funcionaba. Desde la tarea programada de Windows, que no tiene a nadie que exporte nada,
el bot arrancaba operando pero mudo y sin control por chat, y lo hacia en silencio.

Por eso estos tests van contra el CLI y no contra el parser: el parser ya funcionaba.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli import CliDeps, app
from tests.test_cli import write_yaml

TOKEN = "123:TOKEN-DEL-FICHERO"


@pytest.fixture
def cfg_path(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


def telegram():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "aisha_bot"}})
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 55, "type": "private"}, "text": "hola"}}
        ]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(cfg_path, raiz):
    return CliRunner().invoke(
        app, ["telegram-chat-id", "--config", str(cfg_path), "--root", str(raiz)],
        obj=CliDeps(telegram_client=telegram()),
    )


def test_el_token_del_fichero_env_SI_se_usa(cfg_path, tmp_path):
    """La regresion exacta: token en el .env, nada en el entorno, y el comando funciona."""
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    r = run(cfg_path, tmp_path)
    assert r.exit_code == 0, r.output
    assert "55" in r.output


def test_el_env_de_windows_con_CRLF_y_BOM_tambien_vale(cfg_path, tmp_path):
    """El .env real del proyecto se escribio desde Windows. Si el \\r se cuela en el valor,
    Telegram devuelve 404 y el mensaje no dice por que."""
    (tmp_path / ".env").write_text(
        f"# comentario\r\nTELEGRAM_BOT_TOKEN={TOKEN}\r\n", encoding="utf-8-sig"
    )
    assert run(cfg_path, tmp_path).exit_code == 0


def test_sin_fichero_ni_entorno_sigue_diciendo_que_falta(cfg_path, tmp_path):
    r = run(cfg_path, tmp_path)
    assert r.exit_code == 2 and "TELEGRAM_BOT_TOKEN" in r.output


def test_el_entorno_gana_al_fichero(cfg_path, tmp_path, monkeypatch):
    """Lo que configura un servicio de forma explicita no lo pisa un fichero olvidado."""
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=del-fichero\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "del-entorno")
    run(cfg_path, tmp_path)
    import os
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "del-entorno"


def test_el_valor_del_fichero_no_aparece_en_la_salida(cfg_path, tmp_path):
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    assert TOKEN not in run(cfg_path, tmp_path).output


def test_un_env_de_otra_carpeta_no_se_cuela(cfg_path, tmp_path):
    """El .env que se lee es el de --root. Coger el del directorio actual haria que el bot
    se comportara distinto segun desde donde lo arranques."""
    otra = tmp_path / "otra"
    otra.mkdir()
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    assert run(cfg_path, otra).exit_code == 2
