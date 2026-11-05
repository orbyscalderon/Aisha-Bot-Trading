from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.config import BotConfig
from core.engine import Engine
from core.models import (
    ExitReason, Fill, Market, Position, Quote, RejectReason, Side, Signal,
)
from core.portfolio import Portfolio
from core.safety import CircuitBreaker
from ports.market import MarketDataSource, OrderExecutor, QuoteUnavailable, ZeroQuantity
from ports.notifier import Level, Notifier
from ports.signal_source import SignalSource
from storage.audit import AuditLog
from storage.db import init_db
from storage.repository import Repository

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class FakeSource(SignalSource):
    def __init__(self, signals):
        self._signals = signals

    @property
    def name(self):
        return "fake"

    def fetch_trades(self, since):
        return list(self._signals)

    def list_followed(self):
        return ["t1"]


class RaisingSource(FakeSource):
    def __init__(self):
        super().__init__([])

    def fetch_trades(self, since):
        raise RuntimeError("fuente caída")


class FakeData(MarketDataSource):
    # balance = capital TOTAL (contrato de get_balance): abrir posiciones no lo baja.
    def __init__(self, price=Decimal("100"), balance=Decimal("10000")):
        self.price = price
        self.balance = balance
        self.failing = set()

    def get_quote(self, symbol):
        if symbol in self.failing:
            raise QuoteUnavailable(symbol)
        return Quote(symbol, self.price, NOW)

    def get_balance(self):
        return self.balance


class FakeExecutor(OrderExecutor):
    def __init__(self, fail=None, price_factor=Decimal("1.01")):
        self.fail = fail
        self.price_factor = price_factor
        self.orders = []

    def submit_buy(self, order):
        self.orders.append(order)
        if self.fail is not None:
            raise self.fail
        price = order.reference_price * self.price_factor
        qty = (order.amount_usd / price).quantize(Decimal("0.00000001"))
        return Fill(f"f-{order.client_order_id}", order.client_order_id, order.market,
                    order.symbol, Side.BUY, price, qty, Decimal("0.1"), NOW)

    def submit_sell(self, order):
        self.orders.append(order)
        if self.fail is not None:
            raise self.fail
        return Fill(f"f-{order.client_order_id}", order.client_order_id, order.market,
                    order.symbol, Side.SELL, order.reference_price, order.quantity,
                    Decimal("0.1"), NOW, position_id=order.position_id)


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    def send(self, message, level):
        self.sent.append((message, level))


def make_cfg(**over):
    raw = {
        "mode": "paper",
        "sizing": {"strategy": "flat", "flat_amount_usd": 100},
        "exits": {"stop_loss_pct": 10, "primary": "copy_sell"},
        "risk": {"max_usd_per_token": 1000, "max_open_positions": 5, "max_usd_per_day": 5000,
                 "max_daily_loss_usd": 500, "hard_max_order_usd": 1000},
        "cadence": {"entry_cron": "0 * * * *", "exit_interval_seconds": 60},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k] = {**raw[k], **v}
        else:
            raw[k] = v
    return BotConfig.model_validate(raw)


class Env:
    def __init__(self, tmp_path, cfg=None, executor=None, data=None):
        self.conn = init_db(tmp_path / "bot.db")
        self.repo = Repository(self.conn)
        self.audit = AuditLog(self.conn)
        self.root = tmp_path
        self.data = data or FakeData()
        self.executor = executor or FakeExecutor()
        self.notifier = FakeNotifier()
        self.engine = self.build(cfg or make_cfg())

    def build(self, cfg):
        return Engine(
            config=cfg, repo=self.repo, audit=self.audit,
            portfolio=Portfolio(self.repo), data_source=self.data, executor=self.executor,
            notifier=self.notifier, circuit_breaker=CircuitBreaker(3),
            project_root=self.root,
        )

    def events(self):
        return {e.event for e in self.audit.events()}


@pytest.fixture
def make_env(tmp_path):
    envs = []

    def _make(**kw):
        e = Env(tmp_path, **kw)
        envs.append(e)
        return e

    yield _make
    for e in envs:
        e.conn.close()


def sig(id_="s1", trader="t1", symbol="BTC/USDT", ts=NOW, side=Side.BUY):
    return Signal(id_, trader, Market.CEX, symbol, side, ts, None)


def open_pos(env, id_="p1", entry=100, peak=None, symbol="BTC/USDT"):
    p = Position(id_, "s-" + id_, "t1", Market.CEX, symbol, Decimal(entry), Decimal("1"),
                 Decimal(entry), datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
                 Decimal(peak if peak is not None else entry))
    env.repo.save_position(p)
    return p


def test_entry_cycle_opens_position_and_advances_cursor(make_env):
    env = make_env()
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert len(r.opened) == 1 and r.cursor_advanced is True
    assert env.repo.get_cursor("fake") == NOW


def test_position_quantity_comes_from_the_fill_not_the_guard(make_env):
    env = make_env()
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    fill = env.repo.fills_between(NOW, NOW.replace(hour=13))[0]
    assert r.opened[0].quantity == fill.quantity
    assert r.opened[0].entry_price == fill.price == Decimal("101")


def test_cursor_not_advanced_when_source_raises(make_env):
    env = make_env()
    r = env.engine.run_entry_cycle(RaisingSource(), NOW)
    assert r.cursor_advanced is False and env.repo.get_cursor("fake") is None


def test_reprocessed_cycle_does_not_duplicate_position(make_env):
    env = make_env()
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and len(env.repo.open_positions()) == 1
    assert r.rejected[0][1] is RejectReason.DUPLICATE


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-5")])
def test_invalid_quote_rejects_without_opening(make_env, price):
    env = make_env(data=FakeData(price=price))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.INVALID_QUOTE


def test_unavailable_quote_rejects_as_invalid_quote(make_env):
    env = make_env()
    env.data.failing.add("BTC/USDT")
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.INVALID_QUOTE


def test_quantity_rounding_to_zero_rejects_without_reserving(make_env):
    # El brief usaba 50 USD a 120000 con 4 decimales, que da 0.0004 (no cero).
    # Con 5 USD da 0.0000416 y redondea a 0.0000.
    cfg = make_cfg(sizing={"flat_amount_usd": 5},
                   execution={"cex": {"quantity_precision": 4}})
    env = make_env(cfg=cfg, data=FakeData(price=Decimal("120000")))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.rejected[0][1] is RejectReason.ZERO_QUANTITY
    assert env.repo.unconfirmed_intents() == [] and env.executor.orders == []
    assert "order_reserved" not in env.events()


def test_risk_rejection_does_not_consume_client_order_id(make_env):
    env = make_env(cfg=make_cfg(risk={"max_usd_per_token": 50}))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.rejected[0][1] is RejectReason.MAX_USD_PER_TOKEN
    assert "order_reserved" not in env.events()
    # Las condiciones cambian (límite más alto): la misma señal se puede reintentar.
    env.engine = env.build(make_cfg())
    r2 = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert len(r2.opened) == 1


def test_risk_shutdown_stops_cycle_and_reports(make_env):
    env = make_env()
    p = open_pos(env)
    env.repo.close_position(p.id, Decimal("50"), ExitReason.STOP_LOSS, NOW)
    # Venta a -500 USD el mismo día: PnL realizado muy por debajo de -500.
    env.repo.save_fill(Fill("f-x", "ct-x", Market.CEX, "BTC/USDT", Side.SELL, Decimal("-500"),
                            Decimal("1"), Decimal("0"), NOW, position_id=p.id))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1"), sig("s2")]), NOW)
    assert r.shutdown is True and r.opened == []
    assert any(level is Level.CRITICAL for _, level in env.notifier.sent)


def test_protections_block_entries_with_their_own_reason(make_env):
    env = make_env(cfg=make_cfg(protections={"cooldown_minutes": 60}))
    p = open_pos(env, entry=100)
    env.repo.close_position(p.id, Decimal("90"), ExitReason.STOP_LOSS,
                            datetime(2026, 11, 4, 11, 30, tzinfo=UTC))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.COOLDOWN


def test_kill_switch_blocks_entries(make_env):
    env = make_env()
    (env.root / "STOP").touch()
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and r.cursor_advanced is False


def test_kill_switch_does_not_block_exits(make_env):
    env = make_env()
    open_pos(env, entry=100)
    env.data.price = Decimal("80")
    (env.root / "STOP").touch()
    assert len(env.engine.run_exit_cycle(NOW).closed) == 1


def test_not_allowlisted_trader_is_rejected(make_env):
    env = make_env()
    r = env.engine.run_entry_cycle(FakeSource([sig("s1", trader="intruso")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.NOT_IN_ALLOWLIST


def test_hard_cap_applied_before_order_and_audited(make_env):
    cfg = make_cfg(sizing={"flat_amount_usd": 9999},
                   risk={"hard_max_order_usd": 500, "max_usd_per_token": 99999,
                         "max_usd_per_day": 99999})
    env = make_env(cfg=cfg)
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened[0].amount_usd == Decimal("500")
    assert env.executor.orders[0].amount_usd == Decimal("500")
    ev = [e for e in env.audit.events() if e.event == "hard_cap_applied"]
    assert len(ev) == 1 and ev[0].payload["reason"] == RejectReason.HARD_CAP.value
    assert any(level is Level.WARNING for _, level in env.notifier.sent)


def test_hard_cap_silent_when_not_clipping(make_env):
    env = make_env()
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert "hard_cap_applied" not in env.events()


def test_exit_cycle_closes_position_on_stop_loss(make_env):
    env = make_env()
    open_pos(env, entry=100)
    env.data.price = Decimal("80")
    c = env.engine.run_exit_cycle(NOW).closed
    assert c[0].exit_reason is ExitReason.STOP_LOSS and env.repo.open_positions() == []


def test_exit_cycle_updates_peak_price_when_rising(make_env):
    env = make_env()
    open_pos(env, entry=100, peak=100)
    env.data.price = Decimal("150")
    env.engine.run_exit_cycle(NOW)
    assert env.repo.get_position("p1").peak_price == Decimal("150")


def test_exit_cycle_never_lowers_peak_price(make_env):
    env = make_env()
    open_pos(env, entry=100, peak=150)
    env.data.price = Decimal("120")
    env.engine.run_exit_cycle(NOW)
    assert env.repo.get_position("p1").peak_price == Decimal("150")


def test_exit_cycle_survives_missing_quote(make_env):
    env = make_env()
    open_pos(env, "p1", symbol="BTC/USDT")
    open_pos(env, "p2", symbol="ETH/USDT")
    env.data.failing.add("BTC/USDT")
    env.data.price = Decimal("80")
    r = env.engine.run_exit_cycle(NOW)
    assert len(r.errors) == 1 and [p.id for p in r.closed] == ["p2"]
    assert env.repo.get_position("p1").closed_at is None


def test_circuit_breaker_opens_after_three_failed_orders(make_env):
    env = make_env(executor=FakeExecutor(fail=RuntimeError("exchange caído")))
    signals = [sig(f"s{i}", symbol=f"A{i}/USDT") for i in range(3)]
    env.engine.run_entry_cycle(FakeSource(signals), NOW)
    assert env.engine.circuit_breaker.is_open is True
    assert env.repo.unconfirmed_intents() == []  # todos marcados FAILED
    assert "order_failed" in env.events()


def test_open_circuit_breaker_blocks_further_entries(make_env):
    env = make_env(executor=FakeExecutor(fail=RuntimeError("x")))
    env.engine.run_entry_cycle(FakeSource([sig(f"s{i}", symbol=f"A{i}") for i in range(3)]), NOW)
    env.executor.fail = None
    r = env.engine.run_entry_cycle(FakeSource([sig("s9", symbol="Z")]), NOW)
    assert r.opened == [] and r.cursor_advanced is False


def test_executor_zero_quantity_is_a_failed_order(make_env):
    env = make_env(executor=FakeExecutor(fail=ZeroQuantity()))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and env.repo.unconfirmed_intents() == []
    assert "order_failed" in env.events()


def test_every_decision_is_audited(make_env):
    env = make_env()
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert env.events() >= {"signal_accepted", "order_reserved", "position_opened"}


def test_exit_is_audited(make_env):
    env = make_env()
    open_pos(env)
    env.data.price = Decimal("80")
    env.engine.run_exit_cycle(NOW)
    assert "position_closed" in env.events()


ALL_PROTECTIONS = {
    "cooldown_minutes": 600, "stop_loss_streak": 1, "streak_pause_hours": 24,
    "max_drawdown_pct": 1, "max_usd_per_group": 1,
}


def _blocked_env(make_env, exits=None):
    """Entorno con todas las protecciones activas y una racha de stop-loss reciente."""
    over = {"protections": ALL_PROTECTIONS}
    if exits:
        over["exits"] = exits
    env = make_env(cfg=make_cfg(**over))
    old = open_pos(env, "old", symbol="ETH/USDT")
    env.repo.close_position(old.id, Decimal("80"), ExitReason.STOP_LOSS,
                            datetime(2026, 11, 4, 11, 0, tzinfo=UTC))
    return env


def test_protections_really_block_entries_in_that_state(make_env):
    # Precondición del test de salidas: el estado bloquea de verdad las entradas.
    env = _blocked_env(make_env)
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.STOP_LOSS_STREAK


def test_exit_cycle_closes_positions_even_when_all_protections_block(make_env):
    env = _blocked_env(make_env)
    open_pos(env, "p1", entry=100)
    env.data.price = Decimal("80")
    r = env.engine.run_exit_cycle(NOW)
    assert len(r.closed) == 1
    assert r.closed[0].exit_reason is ExitReason.STOP_LOSS


def test_exit_cycle_closes_on_take_profit_even_when_protections_block(make_env):
    env = _blocked_env(make_env, exits={"primary": "take_profit", "take_profit_pct": 20})
    open_pos(env, "p1", entry=100)
    env.data.price = Decimal("130")
    r = env.engine.run_exit_cycle(NOW)
    assert [p.exit_reason for p in r.closed] == [ExitReason.TAKE_PROFIT]


def test_exit_cycle_closes_on_time_stop_even_when_protections_block(make_env):
    env = _blocked_env(make_env, exits={"primary": "time_stop", "time_stop_hours": 1})
    open_pos(env, "p1", entry=100)  # abierta a las 09:00, ahora 12:00
    r = env.engine.run_exit_cycle(NOW)
    assert [p.exit_reason for p in r.closed] == [ExitReason.TIME_STOP]


def test_exit_cycle_never_consults_protections(make_env, monkeypatch):
    calls = []
    import core.engine as engine_module

    def spy(*a, **k):
        calls.append((a, k))
        raise AssertionError("las salidas no deben consultar protecciones")

    monkeypatch.setattr(engine_module, "evaluate_protections", spy)
    env = make_env()
    open_pos(env, "p1", entry=100)
    env.data.price = Decimal("80")
    assert len(env.engine.run_exit_cycle(NOW).closed) == 1
    assert calls == []


def test_peak_equity_initialised_to_current_equity(make_env):
    env = make_env()
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert Decimal(env.repo.get_state("peak_equity_usd")) == Decimal("10000")


def test_higher_equity_raises_the_persisted_peak(make_env):
    env = make_env()
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    env.data.balance = Decimal("12000")
    env.engine.run_entry_cycle(FakeSource([sig("s2", symbol="ETH/USDT")]), NOW)
    assert Decimal(env.repo.get_state("peak_equity_usd")) == Decimal("12000")


def test_lower_equity_does_not_lower_the_persisted_peak(make_env):
    env = make_env()
    env.repo.set_state("peak_equity_usd", "15000")
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert Decimal(env.repo.get_state("peak_equity_usd")) == Decimal("15000")


def test_drawdown_protection_survives_an_engine_restart(make_env):
    # Pico guardado 15000, capital actual 10000: drawdown 33 % > 10 %.
    env = make_env(cfg=make_cfg(protections={"max_drawdown_pct": 10}))
    env.repo.set_state("peak_equity_usd", "15000")
    restarted = env.build(env.engine._cfg)  # motor nuevo, mismo repositorio
    r = restarted.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.MAX_DRAWDOWN


def test_shutdown_audits_and_warns_about_discarded_signals(make_env):
    env = make_env()
    p = open_pos(env)
    env.repo.close_position(p.id, Decimal("50"), ExitReason.STOP_LOSS, NOW)
    env.repo.save_fill(Fill("f-x", "ct-x", Market.CEX, "BTC/USDT", Side.SELL, Decimal("-500"),
                            Decimal("1"), Decimal("0"), NOW, position_id=p.id))
    signals = [sig("s1"), sig("s2", symbol="ETH/USDT"), sig("s3", symbol="SOL/USDT")]
    env.engine.run_entry_cycle(FakeSource(signals), NOW)
    ev = [e for e in env.audit.events() if e.event == "signals_discarded_by_shutdown"]
    assert len(ev) == 1 and ev[0].payload["discarded"] == 2
    assert any(level is Level.WARNING and "2" in msg for msg, level in env.notifier.sent)


def test_same_signal_reprocessed_with_a_different_now_does_not_duplicate(make_env):
    # Caída tras abrir y antes de avanzar el cursor: al reiniciar llega la misma
    # señal con otro reloj. El identificador no depende del reloj.
    env = make_env()
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    from datetime import timedelta
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW + timedelta(minutes=1))
    assert r.opened == [] and len(env.repo.open_positions()) == 1
    assert len(env.executor.orders) == 1
    assert r.rejected[0][1] is RejectReason.DUPLICATE


def test_failed_exit_is_retried_next_cycle_and_closes(make_env):
    from datetime import timedelta
    env = make_env(executor=FakeExecutor(fail=RuntimeError("exchange caído")))
    open_pos(env, entry=100)
    env.data.price = Decimal("80")
    r1 = env.engine.run_exit_cycle(NOW)
    assert r1.closed == [] and len(r1.errors) == 1
    env.executor.fail = None
    r2 = env.engine.run_exit_cycle(NOW + timedelta(minutes=1))
    assert [p.exit_reason for p in r2.closed] == [ExitReason.STOP_LOSS]
    assert r2.errors == [] and env.repo.open_positions() == []


def test_failed_entry_is_retried_with_same_signal(make_env):
    env = make_env(executor=FakeExecutor(fail=RuntimeError("x")))
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    env.executor.fail = None
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert len(r.opened) == 1


def test_protections_lookback_covers_streak_window_plus_pause(make_env):
    from datetime import timedelta
    env = make_env(cfg=make_cfg(protections={
        "stop_loss_streak": 1, "streak_window_hours": 24, "streak_pause_hours": 48}))
    seen = []
    real = env.engine._portfolio.closed_positions_since
    env.engine._portfolio.closed_positions_since = lambda since: (seen.append(since), real(since))[1]
    env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert seen == [NOW - timedelta(hours=72)]


def test_stop_inside_window_blocks_for_the_whole_long_pause(make_env):
    from datetime import timedelta
    env = make_env(cfg=make_cfg(protections={
        "stop_loss_streak": 1, "streak_window_hours": 24, "streak_pause_hours": 48}))
    p = open_pos(env, "old", entry=100, symbol="ETH/USDT")
    env.repo.close_position(p.id, Decimal("80"), ExitReason.STOP_LOSS, NOW - timedelta(hours=20))
    r = env.engine.run_entry_cycle(FakeSource([sig("s1")]), NOW)
    assert r.rejected[0][1] is RejectReason.STOP_LOSS_STREAK
