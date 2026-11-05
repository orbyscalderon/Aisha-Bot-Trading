"""Ejecutor simulado: precios reales, dinero falso.

Pide la cotización a la MarketDataSource que recibe (la misma que usaría el
bot en vivo) y simula el relleno. El deslizamiento va SIEMPRE en contra del
operador: un simulador optimista haría ganadoras sobre el papel estrategias
perdedoras y el backtest dejaría de servir para descartarlas.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from core.models import Fill, Order, Side
from ports.market import MarketDataSource, OrderExecutor, ZeroQuantity

_BPS = Decimal(10000)


class PaperExecutor(OrderExecutor):
    def __init__(
        self,
        data_source: MarketDataSource,
        slippage_bps: int,
        fee_bps: int,
        quantity_precision: int,
    ) -> None:
        self._source = data_source
        # Decimal desde el principio: int / int daría float.
        self._slippage = Decimal(slippage_bps) / _BPS
        self._fee_rate = Decimal(fee_bps) / _BPS
        self._quantum = Decimal(1).scaleb(-quantity_precision)

    def submit_buy(self, order: Order) -> Fill:
        quote = self._source.get_quote(order.symbol)
        price = quote.price * (1 + self._slippage)
        # Autoridad sobre la cantidad: se calcula desde el precio ya deslizado.
        quantity = (order.amount_usd / price).quantize(self._quantum, rounding=ROUND_DOWN)
        return self._fill(order, Side.BUY, price, quantity, quote.timestamp)

    def submit_sell(self, order: Order) -> Fill:
        quote = self._source.get_quote(order.symbol)
        price = quote.price * (1 - self._slippage)
        assert order.quantity is not None  # garantizado por Order.__post_init__
        quantity = order.quantity.quantize(self._quantum, rounding=ROUND_DOWN)
        return self._fill(order, Side.SELL, price, quantity, quote.timestamp)

    def _fill(self, order: Order, side: Side, price: Decimal, quantity: Decimal, timestamp) -> Fill:
        if quantity <= 0:
            raise ZeroQuantity(
                f"{order.client_order_id}: cantidad cero a precio {price} "
                f"({order.symbol}, {side.value})"
            )
        return Fill(
            id=str(uuid4()),
            client_order_id=order.client_order_id,
            market=order.market,
            symbol=order.symbol,
            side=side,
            price=price,
            quantity=quantity,
            fee_usd=price * quantity * self._fee_rate,
            timestamp=timestamp,
            position_id=order.position_id,
        )
