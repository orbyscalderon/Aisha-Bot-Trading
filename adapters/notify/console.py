"""Notificador de consola: misma garantía de modo que el de Telegram."""

from __future__ import annotations

import logging

from core.models import Mode
from core.safety import mode_prefix
from ports.notifier import Level, Notifier

logger = logging.getLogger(__name__)


class ConsoleNotifier(Notifier):
    def __init__(self, mode: Mode) -> None:
        self._mode = mode

    def send(self, message: str, level: Level) -> None:
        try:
            print(f"{mode_prefix(self._mode)} [{level.value}] {message}")
        except Exception as exc:  # noqa: BLE001 - nada de aquí puede interrumpir al motor
            logger.error("no se pudo escribir la notificación en consola: %s", type(exc).__name__)
