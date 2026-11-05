from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.config import BotConfig, ProtectionsConfig
from core.models import ExitReason, Market, Position, RejectReason
from core.rules.protections import (
    ProtectionState,
    ProtectionVerdict,
    evaluate_protections,
    group_of,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
D50, D60, D880, D900, D1000 = (Decimal(x) for x in ("50", "60", "880", "900", "1000"))


def cfg(cooldown=0, streak=0, window=24, pause=24, max_dd=0, max_group=0):
    return ProtectionsConfig(
        cooldown_minutes=cooldown,
        stop_loss_streak=streak,
        streak_window_hours=window,
        streak_pause_hours=pause,
        max_drawdown_pct=Decimal(max_dd),
        max_usd_per_group=Decimal(max_group),
    )


def closed(symbol, reason, ago, win=False, n=0):
    entry = Decimal("100")
    return Position(
        id=f"p-{symbol}-{n}", signal_id=f"s-{n}", trader_id="t", market=Market.CEX,
        symbol=symbol, entry_price=entry, quantity=Decimal("1"), amount_usd=entry,
        opened_at=NOW - ago - timedelta(hours=1), peak_price=entry,
        closed_at=NOW - ago, exit_reason=reason,
        exit_price=Decimal("110") if win else Decimal("90"),
    )


def state(closed_positions=(), peak=D1000, equity=D1000, exposure=None, groups=None):
    return ProtectionState(
        closed_positions=tuple(closed_positions), peak_equity_usd=peak, equity_usd=equity,
        exposure_by_symbol=exposure or {}, groups=groups or {},
    )


def state_with_loss(symbol, minutes_ago):
    return state([closed(symbol, ExitReason.COPY_SELL, timedelta(minutes=minutes_ago))])


def state_with_win(symbol, minutes_ago):
    return state([closed(symbol, ExitReason.COPY_SELL, timedelta(minutes=minutes_ago), win=True)])


def state_with_stops(n, hours):
    return state([closed(f"T{i}", ExitReason.STOP_LOSS, timedelta(hours=hours), n=i) for i in range(n)])


def state_with_stops_at(*hours_ago):
    return state([closed(f"T{i}", ExitReason.STOP_LOSS, timedelta(hours=h), n=i) for i, h in enumerate(hours_ago)])


def state_with_closes(reason, n):
    return state([closed(f"T{i}", reason, timedelta(hours=1), n=i) for i in range(n)])


def test_cooldown_blocks_reentry_after_a_loss():
    v = evaluate_protections("BONK", D50, state_with_loss("BONK", minutes_ago=30), cfg(cooldown=60), NOW)
    assert v.blocked and v.reason is RejectReason.COOLDOWN
    assert v.until == NOW + timedelta(minutes=30)


def test_cooldown_expires():
    v = evaluate_protections("BONK", D50, state_with_loss("BONK", minutes_ago=61), cfg(cooldown=60), NOW)
    assert v.blocked is False


def test_cooldown_does_not_apply_to_a_profitable_close():
    v = evaluate_protections("BONK", D50, state_with_win("BONK", minutes_ago=5), cfg(cooldown=60), NOW)
    assert v.blocked is False


def test_cooldown_is_per_symbol():
    v = evaluate_protections("WIF", D50, state_with_loss("BONK", minutes_ago=5), cfg(cooldown=60), NOW)
    assert v.blocked is False


def test_stop_loss_streak_pauses_all_entries():
    v = evaluate_protections("ETH/USDT", D50, state_with_stops(3, hours=20), cfg(streak=3, window=24), NOW)
    assert v.blocked and v.reason is RejectReason.STOP_LOSS_STREAK
    assert v.until == NOW - timedelta(hours=20) + timedelta(hours=24)


def test_stop_loss_streak_ignores_stops_outside_the_window():
    v = evaluate_protections("ETH/USDT", D50, state_with_stops(3, hours=30), cfg(streak=3, window=24), NOW)
    assert v.blocked is False


def test_stop_loss_streak_pause_expires():
    # la racha sigue en ventana pero la pausa ya terminó
    v = evaluate_protections("ETH/USDT", D50, state_with_stops(3, hours=20), cfg(streak=3, window=24, pause=10), NOW)
    assert v.blocked is False


def test_only_stop_loss_closes_count_towards_the_streak():
    v = evaluate_protections("ETH/USDT", D50, state_with_closes(ExitReason.TIME_STOP, 3), cfg(streak=3), NOW)
    assert v.blocked is False


def test_max_drawdown_blocks_entries():
    v = evaluate_protections("ETH/USDT", D50, state(peak=D1000, equity=D880), cfg(max_dd=10), NOW)
    assert v.blocked and v.reason is RejectReason.MAX_DRAWDOWN


def test_max_drawdown_allows_exactly_at_threshold():
    assert evaluate_protections("ETH/USDT", D50, state(peak=D1000, equity=D900), cfg(max_dd=10), NOW).blocked is False


def test_correlated_group_exposure_is_summed():
    st = state(exposure={"BONK": D60}, groups={"solana_memes": ["BONK", "WIF"]})
    v = evaluate_protections("WIF", D60, st, cfg(max_group=100), NOW)
    assert v.blocked and v.reason is RejectReason.MAX_USD_PER_GROUP


def test_symbols_in_different_groups_do_not_add_up():
    st = state(exposure={"BONK": D60}, groups={"solana_memes": ["BONK"], "majors": ["ETH/USDT"]})
    assert evaluate_protections("ETH/USDT", D60, st, cfg(max_group=100), NOW).blocked is False


def test_group_exposure_exactly_at_limit_is_allowed():
    st = state(exposure={"BONK": D50}, groups={"g": ["BONK", "WIF"]})
    assert evaluate_protections("WIF", D50, st, cfg(max_group=100), NOW).blocked is False


def test_ungrouped_symbol_is_its_own_group():
    assert group_of("ETH/USDT", {"solana_memes": ["BONK"]}) == "ETH/USDT"


def test_group_of_returns_the_group_name():
    assert group_of("BONK", {"solana_memes": ["BONK", "WIF"]}) == "solana_memes"


def test_streak_is_evaluated_before_drawdown():
    st = state([closed(f"T{i}", ExitReason.STOP_LOSS, timedelta(hours=1), n=i) for i in range(3)],
               peak=D1000, equity=D880)
    v = evaluate_protections("BONK", D50, st, cfg(streak=3, max_dd=10, cooldown=60), NOW)
    assert v.reason is RejectReason.STOP_LOSS_STREAK


def test_everything_disabled_by_default():
    st = state_with_stops(5, hours=1)
    assert evaluate_protections("X", D50, st, ProtectionsConfig(), NOW) == ProtectionVerdict(False)


def test_verdict_is_frozen():
    with pytest.raises(FrozenInstanceError):
        ProtectionVerdict(False).blocked = True  # type: ignore[misc]


def test_cooldown_exactly_at_expiry_does_not_block():
    v = evaluate_protections("BONK", D50, state_with_loss("BONK", minutes_ago=60), cfg(cooldown=60), NOW)
    assert v.blocked is False


def test_streak_pause_ending_exactly_now_does_not_block():
    # último stop hace 12 h con pausa de 12 h: until == now
    v = evaluate_protections("X", D50, state_with_stops(3, hours=12), cfg(streak=3, window=24, pause=12), NOW)
    assert v.blocked is False


def test_stops_exactly_a_window_apart_are_not_a_streak():
    # el primero y el tercero distan exactamente la ventana: desigualdad estricta
    st = state_with_stops_at(1, 13, 25)
    assert evaluate_protections("X", D50, st, cfg(streak=3, window=24, pause=48), NOW).blocked is False


def test_pause_longer_than_window_keeps_blocking_after_the_window():
    # 3 stops hace 30 h: ya fuera de la ventana de 24 h, pero la pausa de 48 h manda
    v = evaluate_protections("X", D50, state_with_stops(3, hours=30), cfg(streak=3, window=24, pause=48), NOW)
    assert v.blocked and v.reason is RejectReason.STOP_LOSS_STREAK
    assert v.until == NOW - timedelta(hours=30) + timedelta(hours=48)


def test_long_pause_expires_after_pause_hours():
    v = evaluate_protections("X", D50, state_with_stops(3, hours=49), cfg(streak=3, window=24, pause=48), NOW)
    assert v.blocked is False


def test_pause_shorter_than_window_is_unchanged():
    v = evaluate_protections("X", D50, state_with_stops(3, hours=5), cfg(streak=3, window=24, pause=12), NOW)
    assert v.blocked and v.until == NOW - timedelta(hours=5) + timedelta(hours=12)


def test_spread_out_stops_never_form_a_streak():
    # 0 h, 30 h y 60 h con ventana 24 h: tres stops, pero nunca tres juntos
    st = state_with_stops_at(1, 31, 61)
    assert evaluate_protections("X", D50, st, cfg(streak=3, window=24, pause=100), NOW).blocked is False


def test_streak_is_anchored_on_the_latest_cluster():
    # un stop viejo suelto no impide detectar el grupo reciente
    st = state_with_stops_at(2, 3, 4, 90)
    v = evaluate_protections("X", D50, st, cfg(streak=3, window=24, pause=48), NOW)
    assert v.blocked and v.until == NOW - timedelta(hours=2) + timedelta(hours=48)


def test_one_stop_short_of_the_streak_does_not_block():
    v = evaluate_protections("X", D50, state_with_stops(2, hours=1), cfg(streak=3), NOW)
    assert v.blocked is False


def test_drawdown_is_evaluated_before_cooldown():
    st = state([closed("BONK", ExitReason.COPY_SELL, timedelta(minutes=5))], peak=D1000, equity=D880)
    v = evaluate_protections("BONK", D50, st, cfg(cooldown=60, max_dd=10), NOW)
    assert v.reason is RejectReason.MAX_DRAWDOWN


def test_cooldown_is_evaluated_before_group_exposure():
    st = state([closed("BONK", ExitReason.COPY_SELL, timedelta(minutes=5))],
               exposure={"WIF": D60}, groups={"g": ["BONK", "WIF"]})
    v = evaluate_protections("BONK", D60, st, cfg(cooldown=60, max_group=100), NOW)
    assert v.reason is RejectReason.COOLDOWN


def test_symbol_in_two_groups_is_rejected_by_config():
    with pytest.raises(ValidationError, match="más de un grupo"):
        BotConfig.model_validate(_minimal_bot(groups={"a": ["BONK"], "b": ["BONK"]}))


def test_bot_config_defaults_protections_to_disabled():
    c = BotConfig.model_validate(_minimal_bot())
    assert c.protections == ProtectionsConfig()
    assert c.groups == {}


def _minimal_bot(**extra):
    return {
        "mode": "paper",
        "sizing": {"strategy": "flat", "flat_amount_usd": "50"},
        "exits": {"primary": "copy_sell"},
        "risk": {"max_usd_per_token": "100", "max_open_positions": 5, "max_usd_per_day": "500",
                 "max_daily_loss_usd": "50", "hard_max_order_usd": "100"},
        "cadence": {"entry_cron": "*/5 * * * *", "exit_interval_seconds": 30},
        **extra,
    }
