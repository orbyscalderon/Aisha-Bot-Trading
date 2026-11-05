from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.config import ExitsConfig
from core.models import ExitReason, Market, Position, Quote
from core.rules.exits import evaluate_exit, next_peak_price

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


def pos(entry=100, peak=None, opened_at=T0):
    entry = Decimal(entry)
    return Position(
        id="p1", signal_id="s1", trader_id="t1", market=Market.CEX, symbol="BTCUSDT",
        entry_price=entry, quantity=Decimal("1"), amount_usd=entry,
        opened_at=opened_at, peak_price=Decimal(peak) if peak is not None else entry,
    )


def q(price):
    return Quote(symbol="BTCUSDT", price=Decimal(price), timestamp=T1)


def cfg(stop_loss_pct=None, primary="copy_sell", tp_pct=None, trailing=False, time_stop_hours=None):
    return ExitsConfig(
        stop_loss_pct=None if stop_loss_pct is None else Decimal(stop_loss_pct),
        primary=primary,
        take_profit_pct=None if tp_pct is None else Decimal(tp_pct),
        trailing=trailing,
        time_stop_hours=time_stop_hours,
    )


def test_stop_loss_triggers_below_threshold():
    # entrada 100, stop 20%, precio 79 -> cierra
    d = evaluate_exit(pos(entry=100), q(79), T1, cfg(stop_loss_pct=20), None)
    assert d.reason is ExitReason.STOP_LOSS and d.fraction == Decimal("1")


def test_stop_loss_not_triggered_exactly_at_threshold():
    # 80 es exactamente -20%: con desigualdad estricta, no dispara
    assert evaluate_exit(pos(entry=100), q(80), T1, cfg(stop_loss_pct=20), None) is None


def test_copy_sell_partial_mirrors_fraction():
    d = evaluate_exit(pos(entry=100), q(105), T1, cfg(primary="copy_sell"), Decimal("0.5"))
    assert d.reason is ExitReason.COPY_SELL and d.fraction == Decimal("0.5")


def test_take_profit_triggers_above_threshold():
    d = evaluate_exit(pos(entry=100), q(131), T1, cfg(primary="take_profit", tp_pct=30), None)
    assert d.reason is ExitReason.TAKE_PROFIT and d.fraction == Decimal("1")


def test_take_profit_not_triggered_exactly_at_threshold():
    assert evaluate_exit(pos(entry=100), q(130), T1, cfg(primary="take_profit", tp_pct=30), None) is None


def test_trailing_uses_peak_not_entry():
    # Pico 200, trailing de 30% (take_profit_pct) -> el umbral baja a 140.
    # 179 sigue por encima de la entrada +30%, pero el trailing deja correr: no cierra.
    c = cfg(primary="take_profit", tp_pct=30, trailing=True)
    assert evaluate_exit(pos(entry=100, peak=200), q(179), T1, c, None) is None
    # 139 cae más de 30% desde el pico (200): cierra, medido desde el pico y no desde la entrada.
    d = evaluate_exit(pos(entry=100, peak=200), q(139), T1, c, None)
    assert d.reason is ExitReason.TAKE_PROFIT


def test_trailing_not_armed_before_take_profit_level_reached():
    # El pico nunca llegó a entrada +30%: la caída desde el pico no es una salida con ganancia.
    c = cfg(primary="take_profit", tp_pct=30, trailing=True)
    assert evaluate_exit(pos(entry=100, peak=110), q(100), T1, c, None) is None


def test_trailing_disabled_by_default():
    assert cfg(primary="take_profit", tp_pct=30).trailing is False


def test_time_stop_triggers_after_hours():
    d = evaluate_exit(pos(opened_at=T0), q(100), T0 + timedelta(hours=49),
                      cfg(primary="time_stop", time_stop_hours=48), None)
    assert d.reason is ExitReason.TIME_STOP and d.fraction == Decimal("1")


def test_time_stop_not_triggered_exactly_at_threshold():
    assert evaluate_exit(pos(opened_at=T0), q(100), T0 + timedelta(hours=48),
                         cfg(primary="time_stop", time_stop_hours=48), None) is None


def test_stop_loss_wins_over_copy_sell():
    # spec §4.4: proteger capital precede a todo lo demás
    d = evaluate_exit(pos(entry=100), q(79), T1, cfg(stop_loss_pct=20, primary="copy_sell"), Decimal("1"))
    assert d.reason is ExitReason.STOP_LOSS


def test_copy_sell_wins_over_take_profit():
    d = evaluate_exit(pos(entry=100), q(140), T1,
                      cfg(stop_loss_pct=20, primary="copy_sell", tp_pct=30), Decimal("1"))
    assert d.reason is ExitReason.COPY_SELL


def test_take_profit_wins_over_time_stop():
    d = evaluate_exit(pos(entry=100, opened_at=T0), q(140), T0 + timedelta(hours=49),
                      cfg(primary="take_profit", tp_pct=30, time_stop_hours=48), None)
    assert d.reason is ExitReason.TAKE_PROFIT


def test_time_stop_coexists_with_primary_as_safety_net():
    # primary es copy_sell y el trader no vendió, pero el tiempo venció
    d = evaluate_exit(pos(opened_at=T0), q(100), T0 + timedelta(hours=49),
                      cfg(primary="copy_sell", time_stop_hours=48), None)
    assert d.reason is ExitReason.TIME_STOP


def test_stop_loss_wins_over_time_stop():
    d = evaluate_exit(pos(entry=100, opened_at=T0), q(79), T0 + timedelta(hours=49),
                      cfg(stop_loss_pct=20, primary="copy_sell", time_stop_hours=48), None)
    assert d.reason is ExitReason.STOP_LOSS


def test_non_primary_conditions_are_ignored():
    # primary copy_sell: el take-profit configurado no actúa si el trader no vendió
    assert evaluate_exit(pos(entry=100), q(140), T1,
                         cfg(primary="copy_sell", tp_pct=30), None) is None


def test_no_exit_when_nothing_triggers():
    assert evaluate_exit(pos(entry=100), q(101), T1, cfg(stop_loss_pct=20, primary="copy_sell"), None) is None


def test_next_peak_price_only_rises():
    assert next_peak_price(pos(peak=200), q(150)) == Decimal("200")
    assert next_peak_price(pos(peak=200), q(250)) == Decimal("250")
