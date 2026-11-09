"""Tests del escucha de Telegram. httpx real sobre MockTransport: ni red ni esperas."""

from __future__ import annotations

import logging

import httpx
import pytest

from interfaces.chat.telegram_listener import TelegramListener

TOKEN = "123456:SECRETO-DEL-BOT-QUE-NO-DEBE-SALIR"
YO = 123456


class FakeDispatcher:
    def __init__(self, reply="ok"):
        self.seen: list[tuple] = []
        self.reply = reply

    def handle(self, sender, text):
        self.seen.append((sender, text))
        return self.reply(text) if callable(self.reply) else self.reply


def update(uid: int, text: str = "/status", chat: int = YO) -> dict:
    return {"update_id": uid,
            "message": {"message_id": uid, "chat": {"id": chat}, "text": text}}


class Server:
    """Telegram falso: sirve tandas de updates y recoge los sendMessage."""

    def __init__(self, *tandas, fallos=()):
        self.tandas = list(tandas)
        self.fallos = list(fallos)
        self.enviados: list[dict] = []
        self.offsets: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            import json
            self.enviados.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        self.offsets.append(request.url.params.get("offset"))
        if self.fallos:
            fallo = self.fallos.pop(0)
            if isinstance(fallo, Exception):
                raise fallo
            return httpx.Response(fallo, text="boom")
        tanda = self.tandas.pop(0) if self.tandas else []
        return httpx.Response(200, json={"ok": True, "result": tanda})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def make(server, dispatcher=None, **kw):
    return TelegramListener(
        TOKEN, dispatcher or FakeDispatcher(), client=server.client(),
        sleep=lambda _s: None, **kw,
    )


# --- el camino normal ---

def test_un_mensaje_llega_al_dispatcher_y_la_respuesta_vuelve():
    server = Server([update(1, "/status")])
    d = FakeDispatcher("modo: paper")
    make(server, d).poll_once()
    assert d.seen == [(YO, "/status")]
    assert server.enviados[0]["text"] == "modo: paper"
    assert server.enviados[0]["chat_id"] == YO


def test_varios_mensajes_en_una_tanda_se_procesan_en_orden():
    server = Server([update(1, "/status"), update(2, "/positions")])
    d = FakeDispatcher()
    make(server, d).poll_once()
    assert [t for _, t in d.seen] == ["/status", "/positions"]


def test_sin_respuesta_no_se_envia_nada():
    """Un remitente no autorizado: el dispatcher devuelve None y no se contesta."""
    server = Server([update(1)])
    make(server, FakeDispatcher(reply=None)).poll_once()
    assert server.enviados == []


# --- el cursor no retrocede ---

def test_la_segunda_consulta_pide_a_partir_del_ultimo_visto():
    """Sin avanzar el offset, Telegram reenviaría los mismos mensajes y el bot ejecutaría cada
    comando una y otra vez."""
    server = Server([update(7)], [])
    escucha = make(server)
    escucha.poll_once()
    escucha.poll_once()
    assert server.offsets == [None, "8"]


def test_un_mensaje_que_hace_reventar_al_dispatcher_no_bloquea_la_cola():
    """Si un mensaje envenenado impidiera avanzar el cursor, se reprocesaría para siempre."""
    def explota(text):
        raise RuntimeError("boom")

    server = Server([update(3, "/status")], [])
    escucha = make(server, FakeDispatcher(reply=explota))
    escucha.poll_once()
    escucha.poll_once()
    assert server.offsets == [None, "4"]


# --- lo que no es un mensaje de texto ---

@pytest.mark.parametrize("raro", [
    {"update_id": 1},                                             # sin mensaje
    {"update_id": 2, "message": {"chat": {"id": YO}}},            # sin texto (una foto)
    {"update_id": 3, "message": {"text": "hola"}},                # sin chat
    {"update_id": 4, "edited_message": {"chat": {"id": YO}, "text": "/stop"}},
    {"update_id": 5, "message": {"chat": {}, "text": "/stop"}},   # chat sin id
])
def test_lo_que_no_es_un_mensaje_de_texto_se_ignora_sin_romper(raro):
    server = Server([raro], [])
    d = FakeDispatcher()
    escucha = make(server, d)
    escucha.poll_once()
    assert d.seen == []
    escucha.poll_once()
    assert server.offsets[1] is not None  # pero el cursor avanza igual


def test_una_respuesta_malformada_de_telegram_no_rompe_el_bucle():
    server = Server()
    server.tandas = [None]  # 'result' que no es una lista
    assert make(server).poll_once() == 0


# --- la red falla ---

def test_un_error_http_no_tumba_el_escucha():
    server = Server([update(1)], fallos=[500])
    assert make(server).poll_once() == 0


def test_un_fallo_de_red_no_tumba_el_escucha():
    server = Server([update(1)], fallos=[httpx.ConnectError("sin red")])
    assert make(server).poll_once() == 0


def test_tras_un_fallo_el_cursor_no_avanza_y_el_mensaje_se_recupera():
    """Un corte de red no debe perder un mensaje: Telegram lo reenvía si no se confirmó."""
    server = Server([update(9, "/status")], fallos=[500])
    d = FakeDispatcher()
    escucha = make(server, d)
    escucha.poll_once()      # falla
    escucha.poll_once()      # ahora sí
    assert [t for _, t in d.seen] == ["/status"]


# --- el token no se filtra ---

def test_el_token_no_aparece_en_los_registros(caplog):
    server = Server([update(1)], fallos=[httpx.ConnectError(f"fallo contra bot{TOKEN}/getUpdates")])
    with caplog.at_level(logging.DEBUG):
        make(server).poll_once()
    assert TOKEN not in caplog.text
    assert "SECRETO" not in caplog.text


def test_el_token_no_aparece_al_fallar_el_envio(caplog):
    server = Server([update(1)])
    d = FakeDispatcher("hola")

    def envio_roto(request):
        if request.url.path.endswith("/sendMessage"):
            raise httpx.ConnectError(f"caido en bot{TOKEN}/sendMessage")
        return server(request)

    escucha = TelegramListener(
        TOKEN, d, client=httpx.Client(transport=httpx.MockTransport(envio_roto)),
        sleep=lambda _s: None,
    )
    with caplog.at_level(logging.DEBUG):
        escucha.poll_once()
    assert TOKEN not in caplog.text
