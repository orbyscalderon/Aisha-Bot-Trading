"""Tests de los notificadores. Ninguno hace peticiones reales: el cliente se inyecta."""

import logging

import httpx
import pytest

from adapters.notify.console import ConsoleNotifier
from adapters.notify.telegram import TelegramNotifier
from core.models import Mode
from ports.notifier import Level, Notifier

SECRET = "123456:SECRET-TOKEN-abc"


class FakeClient:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = 0
        self.last_url = None
        self.last_json = None

    @property
    def last_text(self):
        return self.last_json["text"]

    def post(self, url, json=None, **kwargs):
        self.calls += 1
        self.last_url = url
        self.last_json = json
        request = httpx.Request("POST", url)
        return httpx.Response(self.status_code, request=request, text="err")


class RaisingClient:
    """Cliente cuyo post lanza `exc`; si es callable se construye con la URL."""

    def __init__(self, exc):
        self.exc = exc

    def post(self, url, json=None, **kwargs):
        raise self.exc(url) if callable(self.exc) else self.exc


def test_message_is_prefixed_with_mode():
    # spec §7.1: TODO mensaje declara el modo
    fake = FakeClient()
    TelegramNotifier("t", "1", Mode.PAPER, client=fake).send("abierta BTC", Level.INFO)
    assert fake.last_text.startswith("mode: paper")
    assert "abierta BTC" in fake.last_text


def test_live_mode_prefix_differs():
    fake = FakeClient()
    TelegramNotifier("t", "1", Mode.LIVE, client=fake).send("abierta BTC", Level.INFO)
    assert fake.last_text.startswith("mode: live")


@pytest.mark.parametrize("level", list(Level))
def test_every_level_is_prefixed(level):
    fake = FakeClient()
    TelegramNotifier("t", "1", Mode.LIVE, client=fake).send("x", level)
    assert fake.last_text.startswith("mode: live")


def test_posts_to_send_message_endpoint_with_chat_id():
    fake = FakeClient()
    TelegramNotifier("tok", "42", Mode.PAPER, client=fake).send("x", Level.INFO)
    assert fake.last_url == "https://api.telegram.org/bottok/sendMessage"
    assert fake.last_json["chat_id"] == "42"


def test_is_a_notifier():
    assert isinstance(TelegramNotifier("t", "1", Mode.PAPER, client=FakeClient()), Notifier)
    assert isinstance(ConsoleNotifier(Mode.PAPER), Notifier)


def test_network_error_does_not_propagate():
    # una notificación caída no puede tumbar un ciclo de trading
    n = TelegramNotifier("t", "1", Mode.PAPER, client=RaisingClient(httpx.ConnectError("caído")))
    n.send("x", Level.INFO)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("caído"),
        httpx.ReadTimeout("lento"),
        httpx.HTTPError("genérico"),
        OSError("socket"),
        RuntimeError("cualquier otra cosa"),
        ValueError("inesperado"),
    ],
)
def test_any_exception_does_not_propagate(exc):
    TelegramNotifier("t", "1", Mode.PAPER, client=RaisingClient(exc)).send("x", Level.CRITICAL)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502])
def test_error_http_status_does_not_propagate(status):
    fake = FakeClient(status_code=status)
    TelegramNotifier("t", "1", Mode.PAPER, client=fake).send("x", Level.WARNING)
    assert fake.calls == 1  # sin reintentos: no se bloquea el ciclo


def test_default_client_has_a_timeout():
    n = TelegramNotifier("t", "1", Mode.PAPER)
    assert n._client.timeout.read is not None  # noqa: SLF001


def test_token_never_appears_in_logs_on_network_error(caplog):
    # el token va en la URL: un error de httpx suele incluirla en su texto
    exc = lambda url: httpx.ConnectError(f"fallo conectando a {url}")  # noqa: E731
    with caplog.at_level(logging.DEBUG):
        TelegramNotifier(SECRET, "1", Mode.PAPER, client=RaisingClient(exc)).send("x", Level.INFO)
    assert caplog.records, "el fallo debe registrarse"
    assert SECRET not in caplog.text
    assert "SECRET-TOKEN" not in caplog.text
    for r in caplog.records:
        assert r.exc_info is None or SECRET not in repr(r.exc_info[1])


@pytest.mark.parametrize("status", [400, 401, 429])
def test_token_never_appears_in_logs_on_http_error(caplog, status):
    with caplog.at_level(logging.DEBUG):
        TelegramNotifier(SECRET, "1", Mode.PAPER, client=FakeClient(status)).send("x", Level.INFO)
    assert caplog.records, "el rechazo debe registrarse"
    assert SECRET not in caplog.text
    assert str(status) in caplog.text


def test_token_not_in_logs_when_exception_is_raise_for_status_style(caplog):
    def exc(url):
        req = httpx.Request("POST", url)
        return httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{url}'",
            request=req,
            response=httpx.Response(401, request=req),
        )

    with caplog.at_level(logging.DEBUG):
        TelegramNotifier(SECRET, "1", Mode.PAPER, client=RaisingClient(exc)).send("x", Level.INFO)
    assert SECRET not in caplog.text


def test_console_notifier_also_prefixes_mode(capsys):
    ConsoleNotifier(Mode.PAPER).send("x", Level.INFO)
    assert capsys.readouterr().out.startswith("mode: paper")


def test_console_notifier_live_prefix(capsys):
    ConsoleNotifier(Mode.LIVE).send("x", Level.CRITICAL)
    assert capsys.readouterr().out.startswith("mode: live")


def test_console_notifier_does_not_propagate_output_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("stdout cerrado")

    monkeypatch.setattr("builtins.print", boom)
    ConsoleNotifier(Mode.PAPER).send("x", Level.INFO)


def _real_client(status=200):
    # Cliente httpx real con transporte simulado: no abre sockets ni resuelve DNS,
    # pero sí atraviesa el logging propio de httpx (que escribe la URL completa).
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))


@pytest.mark.parametrize("status", [200, 401, 429])
def test_token_not_leaked_by_httpx_own_logger(caplog, status):
    with caplog.at_level(logging.INFO):
        TelegramNotifier(SECRET, "1", Mode.PAPER, client=_real_client(status)).send("x", Level.INFO)
    assert any(r.name == "httpx" for r in caplog.records), "httpx debe seguir registrando"
    assert SECRET not in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_encoded_token_in_url_pattern_is_redacted(caplog):
    # forma transformada del token: no coincide literalmente, sí con /bot<...>/
    exc = lambda url: httpx.ConnectError(  # noqa: E731
        "fallo en https://api.telegram.org/bot123%3ASECRET-TOKEN-abc/sendMessage"
    )
    with caplog.at_level(logging.DEBUG):
        TelegramNotifier(SECRET, "1", Mode.PAPER, client=RaisingClient(exc)).send("x", Level.INFO)
    assert "SECRET-TOKEN" not in caplog.text
