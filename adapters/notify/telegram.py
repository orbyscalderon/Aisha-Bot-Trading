"""Notificador de Telegram.

Invariantes:
- Todo mensaje empieza por `mode_prefix(mode)`: imposible creer que se está en
  simulado cuando se opera con dinero real.
- Nada de lo que ocurra aquí (red, 400/401/429, errores inesperados) se propaga:
  un fallo de notificación no puede abortar un ciclo de trading.
- El token va en la URL, así que no se registra: (1) los logs de este módulo solo
  llevan el tipo de excepción o el código HTTP, con el texto limpio de token; y
  (2) un `logging.Filter` instalado en los loggers `httpx` y `httpcore` limpia el
  token de sus registros (httpx escribe la URL completa en INFO en cada petición).
  El filtro es de logger: cubre lo que esos loggers emiten directamente. No cubre
  a un handler que se cuelgue de otro logger ni a un cliente con un hook propio.
"""

from __future__ import annotations

import logging
import re

import httpx

from core.models import Mode
from core.safety import mode_prefix
from ports.notifier import Level, Notifier

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_TIMEOUT_SECONDS = 5.0
_BOT_PATH = re.compile(r"/bot[^/\s'\"]+/")


def _scrub(text: str, token: str = "") -> str:
    if token:
        text = text.replace(token, "***")
    return _BOT_PATH.sub("/bot***/", text)


class _TokenRedactingFilter(logging.Filter):
    """Quita de los registros de httpx el token de cualquier URL de bot de Telegram."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            cleaned = _scrub(message)
            if cleaned != message:
                record.msg = cleaned
                record.args = None
        except Exception:  # noqa: BLE001 - un filtro nunca debe romper el logging
            pass
        return True


def _install_log_filter() -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _TokenRedactingFilter) for f in target.filters):
            target.addFilter(_TokenRedactingFilter())


_install_log_filter()


class TelegramNotifier(Notifier):
    def __init__(
        self,
        token: str,
        chat_id: str,
        mode: Mode,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._mode = mode
        self._client = client if client is not None else httpx.Client(timeout=_TIMEOUT_SECONDS)

    def _redact(self, text: str) -> str:
        return _scrub(text, self._token)

    def send(self, message: str, level: Level) -> None:
        try:
            text = f"{mode_prefix(self._mode)} [{level.value}] {message}"
            url = f"{_API}/bot{self._token}/sendMessage"
            response = self._client.post(url, json={"chat_id": self._chat_id, "text": text})
            status = response.status_code
            if status >= 400:
                # 400 chat_id malo, 401 token inválido, 429 rate limit...: se registra
                # solo el código, nunca la URL ni el cuerpo.
                logger.warning("Telegram rechazó la notificación: HTTP %s", status)
        except Exception as exc:  # noqa: BLE001 - nada de aquí puede interrumpir al motor
            try:
                detail = self._redact(str(exc))
                # Sin exc_info: el traceback y el mensaje podrían contener la URL.
                logger.error(
                    "fallo al enviar la notificación a Telegram: %s: %s",
                    type(exc).__name__,
                    detail.replace(_API, "<telegram>"),
                )
            except Exception:  # noqa: BLE001
                pass
