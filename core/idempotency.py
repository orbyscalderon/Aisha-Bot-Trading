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
from datetime import datetime
from enum import StrEnum

from core.models import ExitReason, Side

PREFIX = "ct-"
HEX_LENGTH = 32  # 3 + 32 = 35 <= 36, límite de Binance y de la mayoría de CEX vía ccxt
# Unit Separator (ASCII 0x1F). Se verifica en client_order_id que no aparezca en
# signal_id, trader_id ni symbol (ids de señal vienen de CSV); la marca de tiempo
# generada no puede contenerlo.
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


def _digest(kind: str, **fields: str) -> str:
    for name, value in fields.items():
        if _SEP in value:
            raise ValueError(
                f"{name} contains the reserved separator ; refusing to build "
                f"an ambiguous client_order_id: {value!r}"
            )
    # FORMATO CONGELADO. NO lleva reloj: el objetivo es que la misma señal
    # reprocesada (p. ej. tras una caída, con otro ``now``) produzca el mismo
    # identificador; con un timestamp la garantía no existiría. ``kind`` separa
    # el espacio de entradas del de salidas.
    raw = _SEP.join((kind, *fields.values()))
    return PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:HEX_LENGTH]


def client_order_id(signal_id: str, trader_id: str, symbol: str, side: Side) -> str:
    """Identificador de ENTRADA: ct- + 32 hex de sha256 (35 caracteres).

    La unicidad la aporta ``signal_id`` (las fuentes lo derivan del contenido de
    la señal): la misma operación repetida más tarde es otra señal.
    """
    return _digest(
        "entry", signal_id=signal_id, trader_id=trader_id, symbol=symbol, side=Side(side).value
    )


def exit_order_id(position_id: str, side: Side, exit_reason: ExitReason) -> str:
    """Identificador de SALIDA: (posición, lado, motivo). Sin reloj."""
    return _digest(
        "exit", position_id=position_id, side=Side(side).value, exit_reason=ExitReason(exit_reason).value
    )
