"""Puerto de fuente de señales."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models import Signal


class SignalSource(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def fetch_trades(self, since: datetime) -> list[Signal]: ...

    @abstractmethod
    def list_followed(self) -> list[str]: ...
