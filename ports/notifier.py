"""Puerto de notificaciones."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum


class Level(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Notifier(ABC):
    @abstractmethod
    def send(self, message: str, level: Level) -> None: ...
