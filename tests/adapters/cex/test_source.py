from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ccxt
import pytest

from adapters.cex.source import CexCsvSignalSource, CexDataSource
from core.models import Market, Side
from ports.market import QuoteUnavailable

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)


class FakeCcxt:
    """Cliente falso: nunca toca la red."""

    def __init__(
        self, ticker=None, fail_times=0, error=None, balance=None, tickers=None, markets=None
    ):
        self.markets = markets
        self.ticker = ticker
        self.fail_times = fail_times
        self.error = error
        self.balance = balance
        self.tickers = tickers or {}
        self.call_count = 0

    def fetch_ticker(self, symbol):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise self.error("boom")
        if symbol in self.tickers:
            value = self.tickers[symbol]
            if isinstance(value, type) and issubclass(value, Exception):
                raise value("boom")
            return value
        return self.ticker

    def fetch_balance(self):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise self.error("boom")
        return self.balance


def source(client, sleeps=None, **kwargs):
    rec = sleeps if sleeps is not None else []
    return CexDataSource.from_client(client, sleep=rec.append, **kwargs)


def test_get_quote_returns_decimal_from_last_price():
    # el precio NUNCA debe pasar por float
    assert source(FakeCcxt({"last": "0.000000012345"})).get_quote("X/USDT").price == Decimal(
        "0.000000012345"
    )


def test_get_quote_converts_float_via_str():
    # ccxt devuelve float: Decimal(0.1) arrastraría 0.1000000000000000055...
    assert source(FakeCcxt({"last": 0.1})).get_quote("X/USDT").price == Decimal("0.1")


def test_get_quote_raises_on_zero_price():
    with pytest.raises(QuoteUnavailable):
        source(FakeCcxt({"last": "0"})).get_quote("X/USDT")


def test_get_quote_raises_on_negative_price():
    with pytest.raises(QuoteUnavailable):
        source(FakeCcxt({"last": -1.5})).get_quote("X/USDT")


def test_get_quote_raises_on_missing_last_price():
    with pytest.raises(QuoteUnavailable):
        source(FakeCcxt({"last": None})).get_quote("X/USDT")


def test_get_quote_raises_on_garbage_price():
    with pytest.raises(QuoteUnavailable):
        source(FakeCcxt({"last": "abc"})).get_quote("X/USDT")


def test_get_quote_wraps_network_error():
    client = FakeCcxt(fail_times=99, error=ccxt.NetworkError)
    with pytest.raises(QuoteUnavailable):
        source(client).get_quote("X/USDT")


def test_get_quote_wraps_bad_symbol_without_retry():
    client = FakeCcxt(fail_times=99, error=ccxt.BadSymbol)
    with pytest.raises(QuoteUnavailable):
        source(client).get_quote("X/USDT")
    assert client.call_count == 1


def test_testnet_flag_enables_sandbox_mode():
    # construir el cliente real de ccxt no hace peticiones
    assert CexDataSource("binance", testnet=True)._client.isSandboxModeEnabled is True
    assert CexDataSource("binance")._client.isSandboxModeEnabled is False


def test_unknown_exchange_id_raises_value_error():
    with pytest.raises(ValueError):
        CexDataSource("no_existe_xyz")


def test_rate_limit_retries_with_backoff_then_succeeds():
    client = FakeCcxt({"last": "100"}, fail_times=2, error=ccxt.RateLimitExceeded)
    sleeps = []
    assert source(client, sleeps).get_quote("X/USDT").price == Decimal("100")
    assert client.call_count == 3
    assert sleeps == [1.0, 2.0]


def test_rate_limit_gives_up_after_max_retries():
    client = FakeCcxt(fail_times=99, error=ccxt.RateLimitExceeded)
    sleeps = []
    with pytest.raises(QuoteUnavailable):
        source(client, sleeps).get_quote("X/USDT")
    assert client.call_count == 3
    assert sleeps == [1.0, 2.0]  # sin espera tras el último intento


def test_backoff_is_capped_at_8_seconds():
    client = FakeCcxt(fail_times=99, error=ccxt.RateLimitExceeded)
    sleeps = []
    src = CexDataSource.from_client(client, sleep=sleeps.append, max_attempts=6)
    with pytest.raises(QuoteUnavailable):
        src.get_quote("X/USDT")
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_get_balance_uses_total_not_free_and_values_open_positions():
    # free solo tiene el efectivo; total incluye la posición abierta en BTC
    balance = {
        "free": {"USDT": 100.0, "BTC": 0.0},
        "total": {"USDT": 100.0, "BTC": 0.5},
    }
    client = FakeCcxt(balance=balance, tickers={"BTC/USDT": {"last": "20000"}})
    assert source(client).get_balance() == Decimal("10100")


def test_get_balance_ignores_zero_assets_and_counts_stables():
    balance = {"total": {"USDT": "50.25", "USDC": 10, "ETH": 0}}
    assert source(FakeCcxt(balance=balance)).get_balance() == Decimal("60.25")


def test_get_balance_wraps_errors():
    # el balance es válido: solo el error de ccxt puede provocar la excepción
    client = FakeCcxt(balance={"total": {"USDT": 1}}, fail_times=99, error=ccxt.AuthenticationError)
    with pytest.raises(QuoteUnavailable) as info:
        source(client).get_balance()
    assert isinstance(info.value.__cause__, ccxt.AuthenticationError)


@pytest.mark.parametrize("error", [TypeError, KeyError, ValueError, OSError])
def test_get_quote_wraps_non_ccxt_exceptions(error):
    client = FakeCcxt(fail_times=99, error=error)
    with pytest.raises(QuoteUnavailable) as info:
        source(client).get_quote("X/USDT")
    assert isinstance(info.value.__cause__, error)
    assert client.call_count == 1


def test_get_balance_wraps_non_ccxt_exceptions():
    client = FakeCcxt(balance={"total": {}}, fail_times=99, error=OSError)
    with pytest.raises(QuoteUnavailable):
        source(client).get_balance()


def test_get_balance_treats_none_amounts_as_zero():
    balance = {"total": {"USDT": 10, "BTC": None, "ETH": None}}
    assert source(FakeCcxt(balance=balance)).get_balance() == Decimal("10")


def test_get_balance_falls_back_to_other_quote_assets():
    balance = {"total": {"USDT": 10, "ABC": 2}}
    client = FakeCcxt(
        balance=balance,
        tickers={"ABC/USDT": ccxt.BadSymbol, "ABC/USDC": {"last": "3"}},
    )
    assert source(client).get_balance() == Decimal("16")


def test_get_balance_skips_asset_without_market_and_warns_once(caplog):
    balance = {"total": {"USDT": 10, "LDBTC": 5}}
    client = FakeCcxt(
        balance=balance,
        markets={"BTC/USDT": {}},  # LDBTC no tiene ningún par
        tickers={"LDBTC/USDT": {"last": "999"}},  # no debe consultarse
    )
    src = source(client)
    with caplog.at_level("WARNING"):
        assert src.get_balance() == Decimal("10")
        assert src.get_balance() == Decimal("10")
    warnings = [r for r in caplog.records if "LDBTC" in r.getMessage()]
    assert len(warnings) == 1


def test_get_balance_skips_asset_when_every_pair_raises_bad_symbol():
    balance = {"total": {"USDT": 10, "FAUCET": 5}}
    client = FakeCcxt(
        balance=balance,
        tickers={f"FAUCET/{q}": ccxt.BadSymbol for q in ("USDT", "USDC", "FDUSD", "BUSD")},
    )
    assert source(client).get_balance() == Decimal("10")


def test_get_balance_transient_error_on_existing_pair_still_raises():
    balance = {"total": {"USDT": 10, "BTC": 1}}
    client = FakeCcxt(
        balance=balance,
        markets={"BTC/USDT": {}},
        tickers={"BTC/USDT": ccxt.NetworkError},
    )
    with pytest.raises(QuoteUnavailable):
        source(client).get_balance()


def test_get_balance_ignore_assets_skips_without_pricing():
    balance = {"total": {"USDT": 10, "DUST": 5}}
    client = FakeCcxt(balance=balance, tickers={"DUST/USDT": ccxt.NetworkError})
    assert source(client, ignore_assets=["DUST"]).get_balance() == Decimal("10")


def write_csv(tmp_path, rows):
    path = tmp_path / "signals.csv"
    lines = ["timestamp,trader_id,symbol,side,amount_usd"] + rows
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def two_rows():
    return [
        f"{T0.isoformat()},t1,BTC/USDT,buy,100.5",
        f"{T1.isoformat()},t1,ETH/USDT,sell,",
    ]


def test_csv_source_parses_signals_after_cursor(tmp_path):
    sigs = CexCsvSignalSource(write_csv(tmp_path, two_rows())).fetch_trades(since=T0)
    assert [s.timestamp for s in sigs] == [T1]
    assert sigs[0].side is Side.SELL
    assert sigs[0].market is Market.CEX
    assert sigs[0].trader_amount_usd is None


def test_csv_source_parses_fields(tmp_path):
    sig = CexCsvSignalSource(write_csv(tmp_path, two_rows())).fetch_trades(None)[0]
    assert (sig.trader_id, sig.symbol, sig.side) == ("t1", "BTC/USDT", Side.BUY)
    assert sig.trader_amount_usd == Decimal("100.5")


def test_csv_source_signal_ids_are_stable_across_reads(tmp_path):
    path = write_csv(tmp_path, two_rows())
    a = [s.id for s in CexCsvSignalSource(path).fetch_trades(None)]
    b = [s.id for s in CexCsvSignalSource(path).fetch_trades(None)]
    assert a == b and len(set(a)) == 2


def test_csv_source_ids_differ_for_different_rows(tmp_path):
    rows = [f"{T0.isoformat()},t1,BTC/USDT,buy,100", f"{T0.isoformat()},t1,BTC/USDT,buy,101"]
    ids = [s.id for s in CexCsvSignalSource(write_csv(tmp_path, rows)).fetch_trades(None)]
    assert len(set(ids)) == 2


def test_csv_source_lists_followed_traders(tmp_path):
    src = CexCsvSignalSource(write_csv(tmp_path, two_rows()))
    assert src.list_followed() == ["t1"]
    assert src.name == "cex"


def test_get_balance_skips_listed_pair_with_null_price_and_warns_once(caplog):
    # el par existe pero su ticker no tiene precio: mercado muerto, persistente
    balance = {"total": {"USDT": 10, "DEAD": 5}}
    client = FakeCcxt(
        balance=balance,
        markets={"DEAD/USDT": {}},
        tickers={"DEAD/USDT": {"last": None}},
    )
    src = source(client)
    with caplog.at_level("WARNING"):
        assert src.get_balance() == Decimal("10")
        assert src.get_balance() == Decimal("10")
    assert len([r for r in caplog.records if "DEAD" in r.getMessage()]) == 1


def test_get_balance_skips_listed_pair_with_zero_price():
    balance = {"total": {"USDT": 10, "DEAD": 5}}
    client = FakeCcxt(
        balance=balance, markets={"DEAD/USDT": {}}, tickers={"DEAD/USDT": {"last": "0"}}
    )
    assert source(client).get_balance() == Decimal("10")


@pytest.mark.parametrize("error", [ccxt.NetworkError, ccxt.RateLimitExceeded, OSError])
def test_get_balance_transient_errors_on_listed_pair_still_raise(error):
    balance = {"total": {"USDT": 10, "BTC": 1}}
    client = FakeCcxt(
        balance=balance, markets={"BTC/USDT": {}}, tickers={"BTC/USDT": error}
    )
    with pytest.raises(QuoteUnavailable):
        source(client).get_balance()


def test_get_balance_unreadable_price_on_listed_pair_still_raises():
    balance = {"total": {"USDT": 10, "BTC": 1}}
    client = FakeCcxt(
        balance=balance, markets={"BTC/USDT": {}}, tickers={"BTC/USDT": {"last": "abc"}}
    )
    with pytest.raises(QuoteUnavailable):
        source(client).get_balance()


def test_keyboard_interrupt_is_not_swallowed():
    client = FakeCcxt(fail_times=99, error=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        source(client).get_quote("X/USDT")


@pytest.mark.parametrize("ticker", [None, "oops", []])
def test_get_balance_unreadable_ticker_response_still_raises(ticker):
    balance = {"total": {"USDT": 10, "BTC": 1}}
    client = FakeCcxt(balance=balance, markets={"BTC/USDT": {}}, tickers={"BTC/USDT": ticker})
    with pytest.raises(QuoteUnavailable):
        source(client).get_balance()
