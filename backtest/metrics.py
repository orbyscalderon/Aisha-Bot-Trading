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
- Win rate usa la convención de quantstats: las operaciones con PnL exactamente
  cero no cuentan ni como ganadas ni como perdidas. La expectancy es la media del
  PnL en USD sobre TODAS las operaciones cerradas (las de PnL cero la diluyen:
  excluirlas la inflaría).
- Heurística de quantstats: una serie no negativa con máximo > 1 se toma por
  PRECIOS y se convierte en variaciones. Unos USD (o unos retornos > 100 %)
  siempre la disparan, así que se evita: ``prepare_returns=False`` donde la API
  lo admite; en Sharpe/Sortino (invariantes a la escala) se reescala la serie;
  y sin ninguna operación perdedora el drawdown es 0 y no se consulta.
- Sharpe, Sortino y volatilidad se calculan sobre retornos DIARIOS (UTC), con
  los días sin cierres a cero, anualizados con 365 periodos (los mercados
  cripto no cierran). Con menos de dos días valen cero: no hay desviación.
- Con cero operaciones cerradas todo vale cero y NO se llama a quantstats: una
  serie vacía le hace devolver NaN.
"""

from __future__ import annotations

import math
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import contextlib

import matplotlib
import pandas as pd
import quantstats as qs
import quantstats.reports as qs_reports

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


def _scale_below_one(returns: pd.Series) -> pd.Series:
    """Reescala una serie no negativa con máximo > 1 para que quantstats no la tome por precios.

    Solo para métricas invariantes a la escala (Sharpe, Sortino): dividir todos
    los retornos por una constante no las altera.
    """
    top = float(returns.max())
    if returns.min() >= 0 and top > 1:
        return returns / (2 * top)
    return returns


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
    # Sin retornos negativos no hay caída posible, y quantstats confundiría la serie
    # con precios si algún retorno supera 1.
    if (returns < 0).any():
        dd_pct = qs.stats.to_drawdown_series(returns)
        peak_usd = equity_usd.cummax().clip(lower=float(starting_equity))
        max_dd_usd = float((-dd_pct * peak_usd).max())
    else:
        max_dd_usd = 0.0

    daily = _daily_returns(trades, starting_equity)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # quantstats avisa de series cortas; ya se acotan abajo
        if len(daily) >= 2:
            scaled = _scale_below_one(daily)  # Sharpe y Sortino no cambian con la escala
            sharpe = qs.stats.sharpe(scaled, periods=PERIODS_PER_YEAR)
            sortino = qs.stats.sortino(scaled, periods=PERIODS_PER_YEAR)
            volatility = qs.stats.volatility(daily, periods=PERIODS_PER_YEAR, prepare_returns=False)
        else:
            sharpe = sortino = volatility = 0.0
        win_rate = qs.stats.win_rate(returns, prepare_returns=False)
    expectancy = sum((t.pnl for t in trades), _ZERO) / len(trades)

    durations = [(t.closed_at - t.position.opened_at).total_seconds() / 3600 for t in trades]
    reasons = Counter(t.position.exit_reason for t in trades if t.position.exit_reason)

    return Metrics(
        total_pnl_usd=sum((t.pnl for t in trades), _ZERO).quantize(_Q4),
        max_drawdown_usd=_dec(max_dd_usd) + _ZERO,
        win_rate=_dec(win_rate),
        expectancy_usd=expectancy.quantize(_Q4),
        sharpe=_dec(sharpe),
        avg_duration_hours=_dec(sum(durations) / len(durations)),
        closes_by_reason=dict(reasons),
        sortino=_dec(sortino),
        volatility=_dec(volatility),
    )


@contextlib.contextmanager
def _agg_backend():
    """Backend sin ventana solo mientras se escribe el informe (el Tk puede fallar y no hace
    falta); se restaura después para no cambiar el proceso del que importa este módulo."""
    previous = matplotlib.get_backend()
    matplotlib.use("Agg")
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            matplotlib.use(previous)


@contextlib.contextmanager
def _no_price_heuristic(returns: pd.Series):
    """Evita que ``qs.reports.html`` reinterprete ``returns`` como precios.

    ``reports.html`` llama a ``utils._prepare_returns`` sobre la serie; con una
    serie no negativa de máximo > 1 aplica variación porcentual. Aquí NO vale
    reescalar (el informe muestra porcentajes absolutos), así que durante la
    llamada ``reports._get_utils`` devuelve un envoltorio cuyo ``_prepare_returns``
    deja pasar la serie tal cual, que ya está limpia (finita, sin zona horaria, rf=0).
    Solo se activa cuando la heurística se dispararía, y se deshace siempre. Depende
    de un detalle interno de quantstats 0.0.86: si cambia, falla alto (AttributeError).
    """
    if not (returns.min() >= 0 and returns.max() > 1):
        yield
        return
    original = qs_reports._get_utils

    class _Utils:
        def __getattr__(self, name):
            return getattr(original(), name)

        @staticmethod
        def _prepare_returns(data, *args, **kwargs):
            return data

    qs_reports._get_utils = lambda: _Utils()
    try:
        yield
    finally:
        qs_reports._get_utils = original


def write_html_report(
    fills: list[Fill], positions: list[Position], path: Path,
    starting_equity: Decimal = DEFAULT_STARTING_EQUITY, title: str = "Backtest",
    banner_html: str | None = None,
) -> None:
    """Informe HTML de quantstats sobre los retornos diarios (``backtest --report``)."""
    trades = _closed_trades(fills, positions)
    if not trades:
        raise ValueError("no hay operaciones cerradas: no hay nada que informar")
    daily = _daily_returns(trades, starting_equity)
    with warnings.catch_warnings(), _agg_backend(), _no_price_heuristic(daily):
        warnings.simplefilter("ignore")
        qs.reports.html(daily, benchmark=None, output=str(path), title=title, periods_per_year=PERIODS_PER_YEAR)
    if banner_html:
        # quantstats no tiene hueco para avisos propios: se inserta tras <body>.
        text = Path(path).read_text(encoding="utf-8")
        text, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + banner_html, text, count=1, flags=re.I)
        if n != 1:
            raise RuntimeError("el informe de quantstats no tiene <body>: no se pudo añadir el aviso")
        Path(path).write_text(text, encoding="utf-8")
