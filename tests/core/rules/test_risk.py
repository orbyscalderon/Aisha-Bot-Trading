import inspect
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from core.config import RiskConfig
from core.models import RejectReason
from core.rules.risk import RiskAction, RiskState, build_risk_state, evaluate_risk

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


def test_build_risk_state_assembles_aggregates_from_caller():
    # Portfolio (Tarea 11) es el único dueño de la agregación; esto solo ensambla
    s = build_risk_state(
        D, {"BTC/USDT": Decimal("80")}, 3,
        spent_usd_today=Decimal("200"), realized_pnl_usd_today=Decimal("-81"),
    )
    assert s == RiskState(
        utc_day=D, spent_usd_today=Decimal("200"), realized_pnl_usd_today=Decimal("-81"),
        open_position_count=3, exposure_by_symbol={"BTC/USDT": Decimal("80")},
    )


def test_build_risk_state_takes_no_repository():
    params = inspect.signature(build_risk_state).parameters
    assert "repo" not in params
    assert "spent_usd_today" in params and "realized_pnl_usd_today" in params


def test_build_risk_state_copies_exposure_read_only():
    exposure = {"BTC/USDT": Decimal("80")}
    s = build_risk_state(D, exposure, 1, Decimal("0"), Decimal("0"))
    exposure["BTC/USDT"] = Decimal("999")
    assert s.exposure_by_symbol["BTC/USDT"] == Decimal("80")
    with pytest.raises(TypeError):
        s.exposure_by_symbol["ETH/USDT"] = Decimal("1")
