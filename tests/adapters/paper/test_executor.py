from datetime import datetime, timezone
from decimal import Decimal

import pytest

from adapters.paper.executor import PaperExecutor
from core.models import Market, Order, Quote, Side
from ports.market import MarketDataSource, OrderExecutor, QuoteUnavailable, ZeroQuantity

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeSource(MarketDataSource):
    def __init__(self, price):
        self.price = Decimal(str(price))
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        return Quote(symbol=symbol, price=self.price, timestamp=TS)

    def get_balance(self):
        return Decimal("0")


def src(price):
    return FakeSource(price)


def order(amount=Decimal("100"), coid="ct-1"):
    return Order(
        client_order_id=coid,
        market=Market.CEX,
        symbol="BTC/USDT",
        side=Side.BUY,
        amount_usd=amount,
        reference_price=Decimal("100"),
    )


def sell_order(quantity=Decimal("1")):
    return Order(
        client_order_id="ct-sell",
        market=Market.CEX,
        symbol="BTC/USDT",
        side=Side.SELL,
        amount_usd=Decimal("100"),
        reference_price=Decimal("100"),
        position_id="pos-1",
        quantity=quantity,
    )


def test_buy_fill_price_includes_slippage_against_the_buyer():
    # 100 con 50 bps -> 100.50: el deslizamiento siempre juega en contra
    assert PaperExecutor(src(100), slippage_bps=50, fee_bps=0, quantity_precision=8) \
        .submit_buy(order(Decimal("100"))).price == Decimal("100.50")


def test_sell_fill_price_includes_slippage_against_the_seller():
    assert PaperExecutor(src(100), slippage_bps=50, fee_bps=0, quantity_precision=8) \
        .submit_sell(sell_order()).price == Decimal("99.50")


def test_fee_is_charged_on_notional():
    # 10 bps de 100 USD = 0.10
    f = PaperExecutor(src(100), 0, fee_bps=10, quantity_precision=8).submit_buy(order(Decimal("100")))
    assert f.fee_usd == Decimal("0.10")


def test_sell_fee_is_charged_on_notional_of_slipped_price():
    # 2 unidades a 99.50 = 199 de nocional; 10 bps = 0.199
    f = PaperExecutor(src(100), 50, fee_bps=10, quantity_precision=8).submit_sell(sell_order(Decimal("2")))
    assert f.fee_usd == Decimal("0.199")
    assert f.quantity == Decimal("2")
    assert f.position_id == "pos-1"


def test_quantity_derived_from_slipped_price_not_quoted_price():
    f = PaperExecutor(src(100), slippage_bps=50, fee_bps=0, quantity_precision=8) \
        .submit_buy(order(Decimal("100.50")))
    assert f.quantity == Decimal("1")


def test_fill_carries_client_order_id():
    assert PaperExecutor(src(100), 0, 0, 8).submit_buy(order(coid="ct-abc")).client_order_id == "ct-abc"


def test_uses_real_quote_from_data_source():
    # spec §3.3: el dinero es falso, los precios no
    source = src(42)
    f = PaperExecutor(source, 0, 0, 8).submit_buy(order(Decimal("42")))
    assert f.price == Decimal("42")
    assert source.calls == ["BTC/USDT"]


def test_raises_when_quantity_rounds_to_zero():
    # el ejecutor es la autoridad sobre la cantidad: si el precio deslizado la anula, falla
    with pytest.raises(ZeroQuantity):
        # 5 / 120000 = 0.0000416..., que con 4 decimales y ROUND_DOWN es 0
        PaperExecutor(src(120000), 0, 0, quantity_precision=4).submit_buy(order(Decimal("5")))


def test_slippage_alone_can_push_quantity_to_zero():
    # sin deslizamiento 1 / 100 = 0.01; con 50 bps 1 / 100.5 = 0.00995 -> 0.00
    assert PaperExecutor(src(100), 0, 0, 2).submit_buy(order(Decimal("1"))).quantity == Decimal("0.01")
    with pytest.raises(ZeroQuantity):
        PaperExecutor(src(100), 50, 0, 2).submit_buy(order(Decimal("1")))


def test_quantity_respects_configured_precision():
    f = PaperExecutor(src(3), 0, 0, quantity_precision=2).submit_buy(order(Decimal("10")))
    assert f.quantity == Decimal("3.33")  # ROUND_DOWN, nunca compra de más


def test_all_amounts_are_decimal_never_float():
    f = PaperExecutor(src(100), 50, 10, 8).submit_buy(order(Decimal("100")))
    assert all(isinstance(v, Decimal) for v in (f.price, f.quantity, f.fee_usd))


def test_buy_fill_has_no_position_id_and_uses_quote_timestamp():
    f = PaperExecutor(src(100), 0, 0, 8).submit_buy(order())
    assert f.position_id is None
    assert f.timestamp == TS
    assert f.side is Side.BUY


def test_fill_ids_are_unique():
    ex = PaperExecutor(src(100), 0, 0, 8)
    assert ex.submit_buy(order()).id != ex.submit_buy(order()).id


def test_quote_unavailable_propagates():
    class Down(FakeSource):
        def get_quote(self, symbol):
            raise QuoteUnavailable(symbol)

    with pytest.raises(QuoteUnavailable):
        PaperExecutor(Down(1), 0, 0, 8).submit_buy(order())


def test_implements_order_executor_port():
    assert isinstance(PaperExecutor(src(1), 0, 0, 8), OrderExecutor)
