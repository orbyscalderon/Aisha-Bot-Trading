"""Puerto de fuente de señales."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime

from core.models import Signal


def signature_signal_id(signature: str, trader_id: str) -> str:
    """Id de señal derivado de la firma de una transacción (única e inmutable en la cadena).

    Una misma firma puede afectar a dos traders seguidos, de ahí el ``trader_id``. Lo usan la
    fuente en vivo y el lector de CSV, para que la misma operación tenga el mismo id en ambos.
    """
    return hashlib.sha256(f"sig\x1f{signature}\x1f{trader_id}".encode("utf-8")).hexdigest()[:32]


class SignalSource(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def fetch_trades(self, since: datetime) -> list[Signal]: ...

    @abstractmethod
    def list_followed(self) -> list[str]: ...
