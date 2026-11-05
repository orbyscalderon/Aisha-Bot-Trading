"""Notificador de Telegram.

Invariantes:
- Todo mensaje empieza por `mode_prefix(mode)`: imposible creer que se está en
  simulado cuando se opera con dinero real.
- Nada de lo que ocurra aquí (red, 400/401/429, errores inesperados) se propaga:
  un fallo de notificación no puede abortar un ciclo de trading.
- El token va en la URL, así que jamás se registra: los logs solo llevan el tipo
  de excepción o el código HTTP, y todo texto se limpia de la URL y del token.
"""

from __future__ import annotations

import logging

import httpx

from core.models import Mode
from core.safety import mode_prefix
from ports.notifier import Level, Notifier

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_TIMEOUT_SECONDS = 5.0


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
        if self._token:
            text = text.replace(self._token, "***")
        return text

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
