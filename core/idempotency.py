"""Idempotencia de órdenes: identificador determinista y estados de intención.

Si una conexión expira sin respuesta no sabemos si la orden se ejecutó. La
misma señal produce siempre el mismo client_order_id y ese identificador se
reserva en la base de datos ANTES de salir a la red (Repository.reserve_intent).
Reprocesar un ciclo tras una caída es entonces inocuo.

Este módulo no importa nada de storage/, adapters/ ni interfaces/.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from core.models import Side

PREFIX = "ct-"
HEX_LENGTH = 32  # 3 + 32 = 35 <= 36, límite de Binance y de la mayoría de CEX vía ccxt
# Unit Separator (ASCII 0x1F): no aparece en ids, símbolos ni marcas de tiempo.
_SEP = "\x1f"


class IntentStatus(StrEnum):
    RESERVED = "RESERVED"
    SENT = "SENT"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    status: IntentStatus
    payload: dict
    created_at: datetime
    error: str | None = None


def client_order_id(
    signal_id: str, trader_id: str, symbol: str, side: Side, cycle_ts: datetime
) -> str:
    """Identificador determinista: ct- + 32 hex de sha256 (35 caracteres)."""
    if cycle_ts.tzinfo is None or cycle_ts.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {cycle_ts!r}")
    ts = cycle_ts.astimezone(timezone.utc).isoformat(timespec="microseconds")
    raw = _SEP.join((signal_id, trader_id, symbol, Side(side).value, ts))
    return PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:HEX_LENGTH]
