"""Cartera: único dueño de toda agregación sobre posiciones y fills guardados.

No mantiene estado propio: lee del ``Repository`` en cada llamada. La base de
datos es la fuente de verdad, de modo que un reinicio no pierde nada.

``build_risk_state`` (reglas de riesgo) solo ensambla lo que esta clase calcula;
tener dos agregaciones de lo mismo en dos sitios es garantía de que diverjan.
Todo importe es ``Decimal``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from core.models import Fill, Position, Quote, Side
from ports.market import MarketUnpriceable
from storage.repository import Repository

_ZERO = Decimal(0)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FOREVER = datetime(2200, 1, 1, tzinfo=timezone.utc)

logger = logging.getLogger(__name__)


def _day_window(day: date) -> tuple[datetime, datetime]:
    """Ventana UTC semiabierta [00:00 del día, 00:00 del siguiente)."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


class Portfolio:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # --- posiciones abiertas ---

    def open_positions(self) -> list[Position]:
        return self._repo.open_positions()

    def open_position_count(self) -> int:
        return len(self._repo.open_positions())

    def closed_positions_since(self, since: datetime) -> list[Position]:
        """Posiciones cerradas desde ``since``: insumo de las protecciones."""
        return self._repo.closed_positions_since(since)

    def position_for_symbol(self, symbol: str) -> Position | None:
        for p in self._repo.open_positions():
            if p.symbol == symbol:
                return p
        return None

    def exposure_by_symbol(self) -> dict[str, Decimal]:
        exposure: dict[str, Decimal] = {}
        for p in self._repo.open_positions():
            exposure[p.symbol] = exposure.get(p.symbol, _ZERO) + p.amount_usd
        return exposure

    def unrealized_pnl_usd(self, quotes: Mapping[str, Quote]) -> Decimal:
        """PnL latente. Un símbolo sin cotización se omite: no rompe el global."""
        total = _ZERO
        for p in self._repo.open_positions():
            quote = quotes.get(p.symbol)
            if quote is None:
                continue
            total += p.unrealized_pnl_usd(quote.price)
        return total

    def total_equity_usd(self, quote_fn: Callable[[str], Quote], initial: Decimal) -> Decimal:
        """Capital simulado: inicial + efectivo de todos los fills + valor a mercado de lo abierto.

        Única implementación (modo simulado y backtest la comparten: si cada uno tuviera la
        suya, divergirían). Política según el fallo de cotización de una posición abierta:
        - MarketUnpriceable (permanente: sin mercado o sin precio): se OMITE su valoración con
          un WARNING. Fallar congelaría TODAS las entradas mientras dure un solo activo ajeno.
        - QuoteUnavailable a secas (transitorio: red, límite de tasa): se PROPAGA. Omitirlo
          bajaría el capital por un parpadeo y encogería tamaños o dispararía el drawdown;
          el siguiente ciclo reintenta. Es la misma política que ya aplica el adaptador de CEX.
        """
        cash = initial
        for f in self._repo.fills_between(_EPOCH, _FOREVER):
            notional = f.price * f.quantity
            cash += (-notional if f.side is Side.BUY else notional) - f.fee_usd
        held = _ZERO
        for p in self._repo.open_positions():
            try:
                held += p.quantity * quote_fn(p.symbol).price
            except MarketUnpriceable as exc:
                logger.warning(
                    "capital: sin cotización para %s (posición %s); se omite su valoración (%s)",
                    p.symbol, p.id, exc,
                )
        return cash + held

    # --- agregados del día (ventana UTC semiabierta) ---

    def _fills_of(self, day: date) -> list[Fill]:
        return self._repo.fills_between(*_day_window(day))

    def spent_usd(self, day: date) -> Decimal:
        """Nocional (precio x cantidad) de las compras del día."""
        return sum(
            (f.price * f.quantity for f in self._fills_of(day) if f.side is Side.BUY), _ZERO
        )

    def realized_pnl_usd(self, day: date) -> Decimal:
        """Por cada venta, (precio - entrada de su posición) x cantidad; menos las
        comisiones de todos los fills del día (conservador: las de entrada
        también son dinero perdido).
        """
        realized = _ZERO
        for f in self._fills_of(day):
            realized -= f.fee_usd
            if f.side is Side.BUY:
                continue
            position = (
                self._repo.get_position(f.position_id) if f.position_id is not None else None
            )
            if position is None:
                # Un PnL calculado a ciegas podría ocultar una pérdida: fallar alto.
                raise ValueError(
                    f"sell fill {f.id!r} references unknown position {f.position_id!r}"
                )
            realized += (f.price - position.entry_price) * f.quantity
        return realized
