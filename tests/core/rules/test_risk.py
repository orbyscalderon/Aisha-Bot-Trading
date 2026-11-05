from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from core.config import RiskConfig
from core.models import Fill, Market, Position, RejectReason, Side
from core.rules.risk import RiskAction, RiskState, build_risk_state, evaluate_risk
from storage.db import init_db
from storage.repository import Repository

UTC = timezone.utc
D = date(2026, 11, 4)


def cfg(max_usd_per_token=1000, max_open_positions=10, max_usd_per_day=10000,
        max_daily_loss_usd=200, hard_max_order_usd=5000):
    return RiskConfig(
        max_usd_per_token=Decimal(max_usd_per_token),
        max_open_positions=max_open_positions,
        max_usd_per_day=Decimal(max_usd_per_day),
        max_daily_loss_usd=Decimal(max_daily_loss_usd),
        hard_max_order_usd=Decimal(hard_max_order_usd),
    )


def state(exposure=None, open_count=0, spent=Decimal("0"), realized=Decimal("0")):
    return RiskState(
        utc_day=D, spent_usd_today=spent, realized_pnl_usd_today=realized,
        open_position_count=open_count, exposure_by_symbol=exposure or {},
    )


def empty_state():
    return state()


@pytest.fixture
def repo(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield Repository(c)
    c.close()


def fill_at(ts, amount, id=None, side=Side.BUY, price=Decimal("100"), position_id=None,
            fee=Decimal("0")):
    return Fill(
        id=id or f"f-{ts.isoformat()}-{side.value}", client_order_id=f"c-{ts.isoformat()}-{side.value}",
        market=Market.CEX, symbol="BTC/USDT", side=side, price=price,
        quantity=Decimal(amount) / price, fee_usd=fee, timestamp=ts, position_id=position_id,
    )


def test_allows_order_within_all_limits():
    assert evaluate_risk(Decimal("50"), "BTC/USDT", empty_state(), cfg()).action is RiskAction.ALLOW


def test_allow_has_no_reason():
    assert evaluate_risk(Decimal("50"), "BTC/USDT", empty_state(), cfg()).reason is None


def test_rejects_when_token_exposure_would_exceed():
    # 80 ya expuesto + 50 = 130 > 100
    v = evaluate_risk(Decimal("50"), "BTC/USDT",
                      state(exposure={"BTC/USDT": Decimal("80")}), cfg(max_usd_per_token=100))
    assert v.action is RiskAction.REJECT and v.reason is RejectReason.MAX_USD_PER_TOKEN


def test_allows_order_exactly_at_token_limit():
    v = evaluate_risk(Decimal("20"), "BTC/USDT",
                      state(exposure={"BTC/USDT": Decimal("80")}), cfg(max_usd_per_token=100))
    assert v.action is RiskAction.ALLOW


def test_token_exposure_is_per_symbol():
    v = evaluate_risk(Decimal("50"), "ETH/USDT",
                      state(exposure={"BTC/USDT": Decimal("80")}), cfg(max_usd_per_token=100))
    assert v.action is RiskAction.ALLOW


def test_rejects_when_max_open_positions_reached():
    v = evaluate_risk(Decimal("50"), "X", state(open_count=5), cfg(max_open_positions=5))
    assert v.action is RiskAction.REJECT and v.reason is RejectReason.MAX_OPEN_POSITIONS


def test_allows_when_one_below_max_open_positions():
    v = evaluate_risk(Decimal("50"), "X", state(open_count=4), cfg(max_open_positions=5))
    assert v.action is RiskAction.ALLOW


def test_rejects_when_daily_spend_would_exceed():
    v = evaluate_risk(Decimal("60"), "X", state(spent=Decimal("950")), cfg(max_usd_per_day=1000))
    assert v.action is RiskAction.REJECT and v.reason is RejectReason.MAX_USD_PER_DAY


def test_allows_order_exactly_at_daily_spend_limit():
    v = evaluate_risk(Decimal("50"), "X", state(spent=Decimal("950")), cfg(max_usd_per_day=1000))
    assert v.action is RiskAction.ALLOW


def test_daily_loss_limit_shuts_down_rather_than_rejects():
    # spec §4.5: no rechaza, para
    v = evaluate_risk(Decimal("10"), "X", state(realized=Decimal("-205")), cfg(max_daily_loss_usd=200))
    assert v.action is RiskAction.SHUTDOWN


def test_shutdown_verdict_has_no_reject_reason():
    v = evaluate_risk(Decimal("10"), "X", state(realized=Decimal("-205")), cfg(max_daily_loss_usd=200))
    assert v.reason is None


def test_daily_loss_exactly_at_limit_does_not_shut_down():
    v = evaluate_risk(Decimal("10"), "X", state(realized=Decimal("-200")), cfg(max_daily_loss_usd=200))
    assert v.action is RiskAction.ALLOW


def test_daily_profit_never_shuts_down():
    v = evaluate_risk(Decimal("10"), "X", state(realized=Decimal("5000")), cfg(max_daily_loss_usd=200))
    assert v.action is RiskAction.ALLOW


def test_shutdown_takes_precedence_over_reject():
    v = evaluate_risk(Decimal("9999"), "X", state(realized=Decimal("-205")),
                      cfg(max_daily_loss_usd=200, max_usd_per_day=10))
    assert v.action is RiskAction.SHUTDOWN


def test_risk_state_is_frozen():
    with pytest.raises(FrozenInstanceError):
        empty_state().spent_usd_today = Decimal("1")


def test_build_risk_state_uses_fixed_utc_day_not_now(repo):
    # Review Focus 2: el día se fija al inicio del ciclo, no se vuelve a leer
    repo.save_fill(fill_at(datetime(2026, 11, 4, 23, 59, tzinfo=UTC), Decimal("100")))
    repo.save_fill(fill_at(datetime(2026, 11, 5, 0, 1, tzinfo=UTC), Decimal("700")))
    assert build_risk_state(repo, date(2026, 11, 4), {}, 0).spent_usd_today == Decimal("100")


def test_build_risk_state_day_window_is_half_open(repo):
    repo.save_fill(fill_at(datetime(2026, 11, 4, 0, 0, tzinfo=UTC), Decimal("10")))
    repo.save_fill(fill_at(datetime(2026, 11, 5, 0, 0, tzinfo=UTC), Decimal("90")))
    s = build_risk_state(repo, D, {}, 0)
    assert s.spent_usd_today == Decimal("10") and s.utc_day == D


def test_build_risk_state_takes_exposure_from_caller(repo):
    # la exposición la agrega Portfolio (Tarea 11), no esta función: una sola agregación
    s = build_risk_state(repo, D, {"BTC/USDT": Decimal("80")}, 3)
    assert s.exposure_by_symbol == {"BTC/USDT": Decimal("80")} and s.open_position_count == 3


def test_build_risk_state_empty_day_is_zero(repo):
    s = build_risk_state(repo, D, {}, 0)
    assert s.spent_usd_today == 0 and s.realized_pnl_usd_today == 0


def test_build_risk_state_realized_pnl_from_sells(repo):
    repo.save_position(Position(
        id="p1", signal_id="s1", trader_id="t1", market=Market.CEX, symbol="BTC/USDT",
        entry_price=Decimal("100"), quantity=Decimal("2"), amount_usd=Decimal("200"),
        opened_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC), peak_price=Decimal("100"),
    ))
    repo.save_fill(fill_at(datetime(2026, 11, 4, 9, 0, tzinfo=UTC), Decimal("200")))
    # vende 2 a 60: pierde 80, más 1 de comisión
    repo.save_fill(fill_at(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), Decimal("120"),
                           side=Side.SELL, price=Decimal("60"), position_id="p1", fee=Decimal("1")))
    s = build_risk_state(repo, D, {}, 0)
    assert s.realized_pnl_usd_today == Decimal("-81")
    assert s.spent_usd_today == Decimal("200")  # solo cuentan las compras


def test_build_risk_state_sell_without_position_fails_loudly(repo):
    repo.save_fill(fill_at(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), Decimal("60"),
                           side=Side.SELL, position_id=None))
    with pytest.raises(ValueError, match="unknown position"):
        build_risk_state(repo, D, {}, 0)
