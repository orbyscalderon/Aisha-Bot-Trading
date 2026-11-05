"""Métricas del backtest.

Las estadísticas estándar (drawdown, Sharpe, Sortino, volatilidad, win rate,
expectancy) las calcula ``quantstats``: tienen convenios sutiles (anualización,
drawdown sobre retornos compuestos, qué cuenta como operación ganadora) y un
número de rendimiento equivocado es peor que no tenerlo. Aquí solo se construye
la serie de retornos y se traduce el resultado a ``Decimal``.

Lo propio del bot y NO delegable es ``closes_by_reason``: dice si los umbrales
de salida están calibrados (si el 90 % de los cierres son por time-stop, los
umbrales nunca se alcanzan). Se cuenta aquí.

Convenios:

- Solo cuentan las posiciones CERRADAS; las abiertas no tienen resultado.
- PnL de una operación = (venta - entrada) x cantidad - comisión de entrada -
  comisión de salida. La comisión de entrada se localiza por la convención del
  motor ``Position.id == "pos-" + client_order_id de la compra``.
- Retorno de una operación = PnL / capital justo antes de cerrarla, con el
  capital inicial ``starting_equity`` (los retornos dependen de esa base).
- Win rate y expectancy usan la convención de quantstats: las operaciones con
  PnL exactamente cero no cuentan ni como ganadas ni como perdidas.
- Sharpe, Sortino y volatilidad se calculan sobre retornos DIARIOS (UTC), con
  los días sin cierres a cero, anualizados con 365 periodos (los mercados
  cripto no cierran). Con menos de dos días valen cero: no hay desviación.
- Con cero operaciones cerradas todo vale cero y NO se llama a quantstats: una
  serie vacía le hace devolver NaN.
"""

from __future__ import annotations

import math
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import quantstats as qs

from core.models import ExitReason, Fill, Position, Side

DEFAULT_STARTING_EQUITY = Decimal("10000")
PERIODS_PER_YEAR = 365
_ZERO = Decimal(0)
_Q4 = Decimal("0.0001")
_POSITION_PREFIX = "pos-"


@dataclass(frozen=True, slots=True)
class Metrics:
    total_pnl_usd: Decimal
    max_drawdown_usd: Decimal
    win_rate: Decimal
    expectancy_usd: Decimal
    sharpe: Decimal
    avg_duration_hours: Decimal
    closes_by_reason: dict[ExitReason, int]
    sortino: Decimal = _ZERO
    volatility: Decimal = _ZERO

    @classmethod
    def zero(cls) -> Metrics:
        return cls(_ZERO, _ZERO, _ZERO, _ZERO, _ZERO, _ZERO, {})


@dataclass(frozen=True, slots=True)
class _Trade:
    position: Position
    closed_at: datetime
    pnl: Decimal


def _dec(value: float) -> Decimal:
    """float de quantstats -> Decimal a 4 decimales; NaN/inf (serie degenerada) -> 0."""
    value = float(value)
    if not math.isfinite(value):
        return _ZERO
    return Decimal(repr(value)).quantize(_Q4)


def _closed_trades(fills: list[Fill], positions: list[Position]) -> list[_Trade]:
    sells = {f.position_id: f for f in fills if f.side is Side.SELL and f.position_id}
    buy_fees = {f.client_order_id: f.fee_usd for f in fills if f.side is Side.BUY}
    trades: list[_Trade] = []
    for p in positions:
        if p.closed_at is None:
            continue
        sell = sells.get(p.id)
        if sell is None:
            # Un PnL a ciegas podría ocultar una pérdida: fallar alto.
            raise ValueError(f"posición cerrada {p.id!r} sin fill de venta")
        entry_key = p.id.removeprefix(_POSITION_PREFIX)
        if entry_key not in buy_fees:
            raise ValueError(f"posición cerrada {p.id!r} sin fill de compra {entry_key!r}")
        pnl = (sell.price - p.entry_price) * sell.quantity - sell.fee_usd - buy_fees[entry_key]
        trades.append(_Trade(p, p.closed_at, pnl))
    trades.sort(key=lambda t: t.closed_at)  # estable: respeta el orden de entrada en empates
    return trades


def _naive_utc(ts: datetime) -> datetime:
    return pd.Timestamp(ts).tz_convert("UTC").tz_localize(None).to_pydatetime()


def _per_trade(trades: list[_Trade], starting_equity: Decimal) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(retornos por operación, PnL en USD, capital tras cada operación en USD)."""
    equity = starting_equity
    returns, pnl_usd, equity_usd = [], [], []
    for t in trades:
        returns.append(float(t.pnl / equity) if equity > 0 else 0.0)
        equity += t.pnl
        pnl_usd.append(float(t.pnl))
        equity_usd.append(float(equity))
    index = pd.DatetimeIndex([_naive_utc(t.closed_at) for t in trades])
    return (pd.Series(returns, index=index), pd.Series(pnl_usd, index=index),
            pd.Series(equity_usd, index=index))


def _daily_returns(trades: list[_Trade], starting_equity: Decimal) -> pd.Series:
    by_day: dict = {}
    for t in trades:
        day = t.closed_at.astimezone(timezone.utc).date()
        by_day[day] = by_day.get(day, _ZERO) + t.pnl
    first, last = min(by_day), max(by_day)
    equity = starting_equity
    out: dict = {}
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        pnl = by_day.get(day, _ZERO)
        out[pd.Timestamp(day)] = float(pnl / equity) if equity > 0 else 0.0
        equity += pnl
    return pd.Series(out, dtype=float)


def compute_metrics(
    fills: list[Fill], positions: list[Position],
    starting_equity: Decimal = DEFAULT_STARTING_EQUITY,
) -> Metrics:
    trades = _closed_trades(fills, positions)
    if not trades:
        return Metrics.zero()

    returns, pnl_usd, equity_usd = _per_trade(trades, starting_equity)

    # Drawdown: el porcentaje lo da quantstats; se pasa a USD con el pico de
    # capital vigente (el capital inicial cuenta como pico: perder primero es caída).
    dd_pct = qs.stats.to_drawdown_series(returns)
    peak_usd = equity_usd.cummax().clip(lower=float(starting_equity))
    max_dd_usd = (-dd_pct * peak_usd).max()

    daily = _daily_returns(trades, starting_equity)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # quantstats avisa de series cortas; ya se acotan abajo
        if len(daily) >= 2:
            sharpe = qs.stats.sharpe(daily, periods=PERIODS_PER_YEAR)
            sortino = qs.stats.sortino(daily, periods=PERIODS_PER_YEAR)
            volatility = qs.stats.volatility(daily, periods=PERIODS_PER_YEAR)
        else:
            sharpe = sortino = volatility = 0.0
        win_rate = qs.stats.win_rate(returns)
        expectancy = qs.stats.avg_return(pnl_usd)  # media del PnL en USD por operación

    durations = [(t.closed_at - t.position.opened_at).total_seconds() / 3600 for t in trades]
    reasons = Counter(t.position.exit_reason for t in trades if t.position.exit_reason)

    return Metrics(
        total_pnl_usd=sum((t.pnl for t in trades), _ZERO).quantize(_Q4),
        max_drawdown_usd=_dec(max_dd_usd),
        win_rate=_dec(win_rate),
        expectancy_usd=_dec(expectancy),
        sharpe=_dec(sharpe),
        avg_duration_hours=_dec(sum(durations) / len(durations)),
        closes_by_reason=dict(reasons),
        sortino=_dec(sortino),
        volatility=_dec(volatility),
    )


def write_html_report(
    fills: list[Fill], positions: list[Position], path: Path,
    starting_equity: Decimal = DEFAULT_STARTING_EQUITY, title: str = "Backtest",
) -> None:
    """Informe HTML de quantstats sobre los retornos diarios (``backtest --report``)."""
    trades = _closed_trades(fills, positions)
    if not trades:
        raise ValueError("no hay operaciones cerradas: no hay nada que informar")
    daily = _daily_returns(trades, starting_equity)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        qs.reports.html(daily, benchmark=None, output=str(path), title=title, periods_per_year=PERIODS_PER_YEAR)
