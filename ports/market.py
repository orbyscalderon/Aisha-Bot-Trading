"""Puertos de mercado: lectura (MarketDataSource) y escritura (OrderExecutor).

Van separados a propósito: el modo simulado combina la fuente de datos REAL
de un mercado con el ejecutor de paper/. Las excepciones viven aquí, no en
los adaptadores, para que el motor pueda capturarlas sin importar adapters/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from core.models import Fill, Order, Quote


class QuoteUnavailable(Exception):
    """No hay cotización utilizable para el símbolo."""


class ZeroQuantity(Exception):
    """La orden resultaría en cantidad cero."""


class MarketDataSource(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_balance(self) -> Decimal:
        """Capital TOTAL de la cuenta, incluido el valor de las posiciones abiertas.

        No es el efectivo libre: abrir una posición no debe bajarlo. El motor lo
        usa para dimensionar y para el pico de la protección de drawdown; con
        efectivo libre, abrir posiciones parecería una pérdida de capital.
        """


class OrderExecutor(ABC):
    @abstractmethod
    def submit_buy(self, order: Order) -> Fill: ...

    @abstractmethod
    def submit_sell(self, order: Order) -> Fill: ...
