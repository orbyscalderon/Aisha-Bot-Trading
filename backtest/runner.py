"""Backtest: reproduce el pasado con EL MISMO motor que opera en vivo.

``run_backtest`` construye un ``Engine`` real con ``HistoricalSource`` y
``PaperExecutor`` y avanza un reloj simulado de ``start`` a ``end`` en pasos de
``step``, llamando a ``run_entry_cycle`` y ``run_exit_cycle`` con ese ``now``.
No hay un segundo motor: si lo hubiera, podrían divergir y el backtest dejaría
de significar nada. Un backtest que miente es peor que no tenerlo.

Determinismo: ``PaperExecutor`` sella los fills con la hora de la cotización y
el motor usa el ``now`` simulado; el reloj de pared no interviene en nada que
entre en las métricas.

Decisiones que el plan no fijaba:

- ``HistoricalSource`` entrega cada señal UNA sola vez (``since <= ts <= reloj``).
  El motor deriva el ``client_order_id`` de la hora del ciclo, así que recibir la
  misma señal en dos ciclos la copiaría dos veces; y sin cursor la primera
  consulta es ``since == now``, de modo que una señal exactamente en ``start``
  debe entrar.
- Ninguna cotización ni señal posterior al reloj es visible (sin mirar al futuro).
- ``get_balance`` es el capital total con posiciones abiertas, como en vivo:
  ``initial_balance`` + efectivo generado por los fills (comisiones incluidas) +
  valor a mercado de lo que sigue abierto. Con un capital plano la protección
  de drawdown no podría dispararse nunca en backtest y este favorecería a las
  estrategias que dependen de ella.
- El resultado informa de las posiciones que siguen abiertas al final y de su
  PnL latente a la última cotización: lo realizado solo puede parecer sano.
- ``PaperExecutor`` modela un mercado: las señales deben ser de un único mercado.
- Base de datos y raíz del proyecto (kill switch ``STOP``) son temporales: el
  backtest no hereda ni contamina el estado del bot real.
"""

from __future__ import annotations

import bisect
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from adapters.paper.executor import PaperExecutor
from backtest.metrics import (
    DEFAULT_STARTING_EQUITY, Metrics, compute_metrics, write_html_report,
)
from core.config import BotConfig
from core.engine import Engine
from core.models import Fill, Market, Position, Quote, Side, Signal
from core.portfolio import Portfolio
from core.safety import CircuitBreaker
from ports.market import MarketDataSource, QuoteUnavailable
from ports.notifier import Level, Notifier
from ports.signal_source import SignalSource
from storage.audit import AuditLog
from storage.db import init_db
from storage.repository import Repository

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FOREVER = datetime(2200, 1, 1, tzinfo=timezone.utc)


class HistoricalSource(SignalSource, MarketDataSource):
    """Señales y cotizaciones grabadas, servidas según un reloj simulado."""

    def __init__(
        self, signals: list[Signal], quotes: list[Quote],
        balance: Decimal = DEFAULT_STARTING_EQUITY,
    ) -> None:
        self._signals = sorted(signals, key=lambda s: s.timestamp)
        self._balance = balance
        self._delivered: set[str] = set()
        self._clock: datetime | None = None
        self._repo: Repository | None = None
        by_symbol: dict[str, list[Quote]] = defaultdict(list)
        for q in sorted(quotes, key=lambda q: q.timestamp):
            by_symbol[q.symbol].append(q)
        self._quotes = dict(by_symbol)
        self._quote_times = {s: [q.timestamp for q in qs] for s, qs in self._quotes.items()}

    def set_time(self, now: datetime) -> None:
        self._clock = now

    def _now(self) -> datetime:
        if self._clock is None:
            raise RuntimeError("HistoricalSource: falta set_time() antes de consultar")
        return self._clock

    # SignalSource
    @property
    def name(self) -> str:
        return "historical"

    def fetch_trades(self, since: datetime) -> list[Signal]:
        now = self._now()
        due = [s for s in self._signals
               if since <= s.timestamp <= now and s.id not in self._delivered]
        self._delivered.update(s.id for s in due)
        return due

    def list_followed(self) -> list[str]:
        return sorted({s.trader_id for s in self._signals})

    # MarketDataSource
    def get_quote(self, symbol: str) -> Quote:
        now = self._now()
        times = self._quote_times.get(symbol)
        if not times:
            raise QuoteUnavailable(symbol)
        i = bisect.bisect_right(times, now)
        if i == 0:
            raise QuoteUnavailable(f"{symbol}: sin cotización a {now.isoformat()}")
        return self._quotes[symbol][i - 1]

    def bind_repository(self, repo: Repository) -> None:
        """Enlaza el repositorio del que se deriva el capital. Sin él, el saldo inicial."""
        self._repo = repo

    def get_balance(self) -> Decimal:
        if self._repo is None:
            return self._balance
        cash = self._balance
        for f in self._repo.fills_between(_EPOCH, _FOREVER):
            notional = f.price * f.quantity
            cash += (-notional if f.side is Side.BUY else notional) - f.fee_usd
        held = sum(
            (p.quantity * self.get_quote(p.symbol).price for p in self._repo.open_positions()),
            Decimal(0),
        )
        return cash + held


class _SilentNotifier(Notifier):
    def send(self, message: str, level: Level) -> None:
        return None


@dataclass(frozen=True)
class BacktestResult:
    metrics: Metrics
    positions: list[Position]
    fills: list[Fill]
    engine_class: type
    starting_equity: Decimal = DEFAULT_STARTING_EQUITY
    # Posiciones sin cerrar al terminar y su PnL latente a la última cotización
    # (sin comisión ni deslizamiento de salida). Las métricas son solo de lo realizado.
    open_positions_count: int = 0
    unrealized_pnl_usd: Decimal = Decimal(0)

    def write_report(self, path: Path, title: str = "Backtest") -> None:
        """Informe HTML de quantstats (lo ofrecerá la CLI como ``backtest --report``)."""
        banner = (
            '<div style="font-family:sans-serif;padding:8px;border:1px solid #c90;margin:8px">'
            f"Posiciones abiertas al final: {self.open_positions_count} | "
            f"PnL latente: {self.unrealized_pnl_usd:.2f} USD "
            "(las métricas de este informe solo cuentan operaciones cerradas)</div>"
        )
        write_html_report(self.fills, self.positions, path, self.starting_equity, title, banner)


def run_backtest(
    config: BotConfig,
    signals: list[Signal],
    quotes: list[Quote],
    start: datetime,
    end: datetime,
    step: timedelta,
    initial_balance: Decimal = DEFAULT_STARTING_EQUITY,
) -> BacktestResult:
    for label, ts in (("start", start), ("end", end)):
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError(f"{label} debe llevar zona horaria: {ts!r}")
    if step <= timedelta(0):
        raise ValueError(f"step debe ser positivo: {step!r}")
    if end < start:
        raise ValueError(f"end ({end}) es anterior a start ({start})")
    markets = {s.market for s in signals}
    if len(markets) > 1:
        raise ValueError(
            "el backtest simula un solo mercado por ejecución; las señales mezclan "
            f"{sorted(m.value for m in markets)}"
        )
    market = next(iter(markets), Market.CEX)
    exec_cfg = config.execution[market]

    source = HistoricalSource(signals, quotes, initial_balance)
    executor = PaperExecutor(
        source, exec_cfg.slippage_bps, exec_cfg.fee_bps, exec_cfg.quantity_precision
    )
    with tempfile.TemporaryDirectory(prefix="backtest-") as tmp:
        root = Path(tmp)
        conn = init_db(root / "backtest.db")
        try:
            repo = Repository(conn)
            source.bind_repository(repo)
            portfolio = Portfolio(repo)
            engine = Engine(
                config=config, repo=repo, audit=AuditLog(conn), portfolio=portfolio,
                data_source=source, executor=executor, notifier=_SilentNotifier(),
                circuit_breaker=CircuitBreaker(3), project_root=root,
            )
            now = start
            while now <= end:
                source.set_time(now)
                engine.run_entry_cycle(source, now)
                engine.run_exit_cycle(now)
                now += step

            source.set_time(end)  # valoración final: última cotización conocida a ``end``
            still_open = repo.open_positions()
            unrealized = sum(
                (p.unrealized_pnl_usd(source.get_quote(p.symbol).price) for p in still_open),
                Decimal(0),
            )
            positions = still_open + repo.closed_positions_since(_EPOCH)
            fills = repo.fills_between(_EPOCH, _FOREVER)
        finally:
            conn.close()

    positions.sort(key=lambda p: p.opened_at)
    return BacktestResult(
        metrics=compute_metrics(fills, positions, initial_balance),
        positions=positions,
        fills=fills,
        engine_class=type(engine),
        starting_equity=initial_balance,
        open_positions_count=len(still_open),
        unrealized_pnl_usd=unrealized,
    )
