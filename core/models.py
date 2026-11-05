"""Modelos del dominio: enums y dataclasses inmutables.

Todo importe monetario y todo precio es Decimal. Este módulo no importa
nada de adapters/, interfaces/ ni backtest/.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Market(StrEnum):
    FOMO = "fomo"
    CEX = "cex"
    SOLANA = "solana"
    STOCKS = "stocks"


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    COPY_SELL = "copy_sell"
    TAKE_PROFIT = "take_profit"
    TIME_STOP = "time_stop"
    MANUAL = "manual"


class RejectReason(StrEnum):
    MAX_USD_PER_TOKEN = "max_usd_per_token"
    MAX_OPEN_POSITIONS = "max_open_positions"
    MAX_USD_PER_DAY = "max_usd_per_day"
    HARD_CAP = "hard_cap"
    INVALID_QUOTE = "invalid_quote"
    ZERO_QUANTITY = "zero_quantity"
    NOT_IN_ALLOWLIST = "not_in_allowlist"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Signal:
    """Operación de un trader seguido, tal como la reporta la fuente."""

    id: str
    trader_id: str
    market: Market
    symbol: str
    side: Side
    timestamp: datetime
    trader_amount_usd: Decimal | None

    def has_trader_amount(self) -> bool:
        # Ausente y cero se tratan igual: no hay importe utilizable.
        return self.trader_amount_usd is not None and self.trader_amount_usd > 0


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: Decimal
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class Order:
    client_order_id: str
    market: Market
    symbol: str
    side: Side
    amount_usd: Decimal
    reference_price: Decimal


@dataclass(frozen=True, slots=True)
class Fill:
    id: str
    client_order_id: str
    market: Market
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    fee_usd: Decimal
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class Position:
    id: str
    signal_id: str
    trader_id: str
    market: Market
    symbol: str
    entry_price: Decimal
    quantity: Decimal
    amount_usd: Decimal
    opened_at: datetime
    peak_price: Decimal
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None
    exit_price: Decimal | None = None

    def unrealized_pnl_usd(self, price: Decimal) -> Decimal:
        return (price - self.entry_price) * self.quantity

    def pct_change(self, price: Decimal) -> Decimal:
        """Variación porcentual con signo respecto a entry_price."""
        return (price - self.entry_price) / self.entry_price * Decimal("100")
