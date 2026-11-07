import pytest

from ports.market import MarketDataSource, OrderExecutor, QuoteUnavailable, ZeroQuantity
from ports.notifier import Level, Notifier
from ports.signal_source import SignalSource


@pytest.mark.parametrize("port", [MarketDataSource, OrderExecutor, SignalSource, Notifier])
def test_ports_are_abstract(port):
    with pytest.raises(TypeError):
        port()


def test_market_data_and_executor_are_distinct_ports():
    assert not issubclass(MarketDataSource, OrderExecutor)
    assert not issubclass(OrderExecutor, MarketDataSource)


def test_exceptions_and_level_exist():
    assert issubclass(QuoteUnavailable, Exception) and issubclass(ZeroQuantity, Exception)
    assert {l.name for l in Level} == {"INFO", "WARNING", "CRITICAL"}
