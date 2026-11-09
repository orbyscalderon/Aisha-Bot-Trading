"""Escucha de Telegram por long polling: recibe mensajes y devuelve lo que diga el dispatcher.

Es la mitad que faltaba. ``adapters/notify/telegram.py`` ya sabía ENVIAR; esto sabe RECIBIR.

Se usa long polling y no un webhook a propósito: un webhook exige una URL pública accesible
desde internet, con certificado, es decir abrir el ordenador de casa al exterior. El polling sale
desde dentro y no necesita nada de eso.

Decisiones que no son obvias:

- **El cursor avanza aunque el mensaje falle.** Telegram reenvía todo update que no se haya
  confirmado con ``offset``. Si un mensaje que hace reventar al dispatcher impidiera avanzar, se
  reprocesaría eternamente: el bot quedaría atascado ejecutando lo mismo una y otra vez.
- **Pero no avanza si falla la PETICIÓN.** Ahí no se ha procesado nada, y Telegram debe reenviar:
  un corte de red no puede tragarse un ``/stop``.
- **Confirmar el offset es lo que da la idempotencia.** Telegram borra en su lado los updates
  confirmados, así que un reinicio no reprocesa comandos antiguos. No hace falta guardar nada.
- **Nada de lo que pase aquí se propaga.** Si un mensaje mal formado pudiera tumbar el bucle,
  cualquiera apagaría el bot mandando ese mensaje.
- **El token va en la URL** (lo exige la API de Telegram), así que no se registra nunca: todo
  texto que se escriba pasa por ``_scrub``, que ya limpia ``/bot<token>/``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

import httpx

from adapters.notify.telegram import _API, _scrub

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
LONG_POLL_SECONDS = 25  # menor que TIMEOUT_SECONDS, para que corte antes el servidor
ERROR_PAUSE_SECONDS = 5.0
MAX_MESSAGE_CHARS = 4000  # el límite de Telegram son 4096; se deja margen


class _Dispatcher(Protocol):
    def handle(self, sender: Any, text: str) -> str | None: ...


class TelegramListener:
    def __init__(
        self,
        token: str,
        dispatcher: _Dispatcher,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
        long_poll_seconds: int = LONG_POLL_SECONDS,
    ) -> None:
        self._token = token
        self._dispatcher = dispatcher
        self._client = client if client is not None else httpx.Client(timeout=TIMEOUT_SECONDS)
        self._sleep = sleep if sleep is not None else __import__("time").sleep
        self._long_poll = long_poll_seconds
        self._offset: int | None = None
        self._running = False

    # --- bucle ---

    def run_forever(self) -> None:
        """Escucha hasta que alguien llame a ``stop()``. Ningún fallo lo interrumpe."""
        self._running = True
        logger.info("escuchando Telegram")
        while self._running:
            if self.poll_once() == 0 and self._running:
                pass  # poll_once ya esperó si hubo error; si no, el long poll ya bloqueó

    def stop(self) -> None:
        self._running = False

    # --- una vuelta ---

    def poll_once(self) -> int:
        """Pide una tanda, la procesa y confirma. Devuelve cuántos mensajes se atendieron."""
        updates = self._get_updates()
        if not updates:
            return 0

        atendidos = 0
        mayor: int | None = None
        for update in updates:
            uid = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(uid, int):
                mayor = uid if mayor is None else max(mayor, uid)
            try:
                if self._process(update):
                    atendidos += 1
            except Exception:  # noqa: BLE001 - un mensaje no puede tumbar el bucle...
                logger.exception("fallo procesando un update de Telegram")
        if mayor is not None:
            # ...y el cursor avanza igualmente, o ese mensaje se reprocesaría para siempre.
            self._offset = mayor + 1
        return atendidos

    def _process(self, update: Any) -> bool:
        if not isinstance(update, dict):
            return False
        # Solo 'message': un 'edited_message' es un mensaje ya atendido que alguien reescribió;
        # volver a ejecutarlo permitiría disparar un comando editando un mensaje viejo.
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        text = message.get("text")
        chat = message.get("chat")
        if not isinstance(text, str) or not isinstance(chat, dict):
            return False
        chat_id = chat.get("id")
        if chat_id is None:
            return False

        reply = self._dispatcher.handle(chat_id, text)
        if reply:
            self._send(chat_id, reply)
        return True

    # --- red ---

    def _get_updates(self) -> list:
        params: dict[str, Any] = {"timeout": self._long_poll}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            response = self._client.get(f"{_API}/bot{self._token}/getUpdates", params=params)
            if response.status_code >= 400:
                # Solo el código: ni la URL ni el cuerpo, que pueden llevar el token.
                logger.warning("Telegram devolvió HTTP %s al pedir mensajes", response.status_code)
                self._sleep(ERROR_PAUSE_SECONDS)
                return []
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            self._log_sin_token("fallo pidiendo mensajes a Telegram", exc)
            self._sleep(ERROR_PAUSE_SECONDS)
            return []
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list):
            logger.warning("Telegram devolvió una respuesta sin lista de mensajes")
            return []
        return result

    def _send(self, chat_id: Any, text: str) -> None:
        try:
            self._client.post(
                f"{_API}/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]},
            )
        except Exception as exc:  # noqa: BLE001 - no poder contestar no justifica morir
            self._log_sin_token("fallo enviando la respuesta a Telegram", exc)

    def _log_sin_token(self, que: str, exc: Exception) -> None:
        # Sin exc_info: el traceback puede contener la URL con el token dentro.
        try:
            detalle = _scrub(str(exc), self._token)
        except Exception:  # noqa: BLE001
            detalle = "<ilegible>"
        logger.error("%s: %s: %s", que, type(exc).__name__, detalle)
