from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.models import (
    ExitReason,
    Fill,
    Market,
    Mode,
    Order,
    Position,
    Quote,
    RejectReason,
    Side,
    Signal,
)

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def pos_at(entry_price: Decimal) -> Position:
    return Position(
        id="p1",
        signal_id="s1",
        trader_id="t1",
        market=Market.CEX,
        symbol="BTC/USDT",
        entry_price=entry_price,
        quantity=Decimal("2"),
        amount_usd=Decimal("200"),
        opened_at=T0,
        peak_price=entry_price,
    )


def signal_with(amount: Decimal | None) -> Signal:
    return Signal(
        id="s1",
        trader_id="t1",
        market=Market.CEX,
        symbol="BTC/USDT",
        side=Side.BUY,
        timestamp=T0,
        trader_amount_usd=amount,
    )


def test_position_unrealized_pnl_usd():
    pos = pos_at(Decimal("100"))
    assert pos.unrealized_pnl_usd(Decimal("110")) == Decimal("20")


def test_position_pct_change_from_entry():
    # entrada 100, precio 80 -> -20%
    assert pos_at(Decimal("100")).pct_change(Decimal("80")) == Decimal("-20")


def test_models_are_frozen():
    with pytest.raises(FrozenInstanceError):
        pos_at(Decimal("100")).entry_price = Decimal("1")


def test_signal_amount_absent_and_zero_are_distinguishable():
    assert signal_with(None).has_trader_amount() is False
    assert signal_with(Decimal("0")).has_trader_amount() is False
    assert signal_with(Decimal("-5")).has_trader_amount() is False
    assert signal_with(Decimal("10")).has_trader_amount() is True


def test_position_optional_fields_default_to_none():
    pos = pos_at(Decimal("100"))
    assert pos.closed_at is None
    assert pos.exit_reason is None
    assert pos.exit_price is None


def test_enums_have_required_members():
    assert {m.name for m in Side} == {"BUY", "SELL"}
    assert {m.name for m in Mode} == {"PAPER", "LIVE"}
    assert {m.name for m in Market} == {"FOMO", "CEX", "SOLANA", "STOCKS"}
    assert {m.name for m in ExitReason} == {
        "STOP_LOSS", "COPY_SELL", "TAKE_PROFIT", "TIME_STOP", "MANUAL",
    }
    assert {m.name for m in RejectReason} == {
        "MAX_USD_PER_TOKEN", "MAX_OPEN_POSITIONS", "MAX_USD_PER_DAY", "HARD_CAP",
        "INVALID_QUOTE", "ZERO_QUANTITY", "NOT_IN_ALLOWLIST", "DUPLICATE",
    }


@pytest.mark.parametrize("cls", [Signal, Position, Order, Fill, Quote])
def test_dataclasses_are_frozen_and_slotted(cls):
    assert cls.__dataclass_params__.frozen is True
    assert hasattr(cls, "__slots__")
    assert fields(cls)


def test_quote_and_order_and_fill_construct_with_decimal_prices():
    quote = Quote(symbol="BTC/USDT", price=Decimal("100"), timestamp=T0)
    order = Order(
        client_order_id="ct-1",
        market=Market.CEX,
        symbol="BTC/USDT",
        side=Side.BUY,
        amount_usd=Decimal("50"),
        reference_price=quote.price,
    )
    fill = Fill(
        id="f1",
        client_order_id=order.client_order_id,
        market=Market.CEX,
        symbol="BTC/USDT",
        side=Side.BUY,
        price=Decimal("100.5"),
        quantity=Decimal("0.4975"),
        fee_usd=Decimal("0.05"),
        timestamp=T0,
    )
    assert isinstance(fill.price, Decimal)
    assert fill.quantity * fill.price <= order.amount_usd
