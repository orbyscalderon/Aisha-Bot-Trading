"""Tests del descargador histórico. El cliente es falso y el temporizador se inyecta:
ningún test toca la red ni duerme en tiempo real."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ccxt
import pytest

from adapters.cex.history import (
    HistoryError,
    SymbolNotFound,
    fetch_candles,
    interval_ms,
    validate_timeframe,
    write_quotes_csv,
)
from core.models import Quote

UTC = timezone.utc
HOUR = 3_600_000
T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
T0_MS = int(T0.timestamp() * 1000)
FAR_NOW = datetime(2027, 1, 1, tzinfo=UTC)


def ms(hours: float) -> int:
    return T0_MS + int(hours * HOUR)


def candle(hour: float, close=100.5):
    return [ms(hour), 1, 2, 0.5, close, 10]


class FakeClient:
    """Sirve velas de una lista, respetando since/limit como un exchange real."""

    rateLimit = 250

    def __init__(self, candles=None, pages=None, timeframes=None, markets=None, has=None):
        self.candles = candles
        self.pages = pages  # respuestas fijas, una por petición (ignoran since)
        self.timeframes = timeframes if timeframes is not None else {"1h": "1h", "1d": "1d"}
        self.markets = markets if markets is not None else {"BTC/USDT": {}, "ETH/USDT": {}}
        self.has = has if has is not None else {"fetchOHLCV": True}
        self.calls: list[dict] = []
        self.served = 0
        self.errors: list[Exception] = []  # se lanzan, en orden, antes de responder

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
        if self.errors:
            raise self.errors.pop(0)
        if self.pages is not None:
            self.served += 1  # una petición fallida no consume página
            return self.pages[self.served - 1] if self.served <= len(self.pages) else []
        rows = [c for c in self.candles if c[0] >= since]
        return rows[:limit]


def run(client, symbol="BTC/USDT", since=T0, until=FAR_NOW, now=FAR_NOW, **kw):
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    result = fetch_candles(client, symbol, "1h", since, until, now=now, **kw)
    return result, sleeps


# --- conversiones ---

def test_close_price_goes_through_str_not_float():
    # 0.1 + 0.2 en float es 0.30000000000000004; vía str conserva lo que dijo el exchange
    client = FakeClient(pages=[[candle(0, close=0.1 + 0.2)]])
    result, _ = run(client)
    assert result.quotes[0].price == Decimal("0.30000000000000004")
    assert isinstance(result.quotes[0].price, Decimal)
    assert Decimal(str(0.1)) == Decimal("0.1")  # y no Decimal(0.1) con su cola binaria


def test_quote_is_stamped_at_candle_close_not_open():
    # La vela de las 00:00 solo se conoce a las 01:00. Sellarla a las 00:00 daría al backtest
    # un precio del futuro (lookahead).
    result, _ = run(FakeClient(pages=[[candle(0)]]))
    assert result.quotes[0].timestamp == T0 + timedelta(hours=1)
    assert result.quotes[0].symbol == "BTC/USDT"


def test_unfinished_candle_is_dropped():
    # la vela de las 02:00 todavía se está formando a las 02:30: su "cierre" es el precio actual
    now = T0 + timedelta(hours=2, minutes=30)
    result, _ = run(FakeClient(pages=[[candle(0), candle(1), candle(2)]]), now=now)
    assert [q.timestamp for q in result.quotes] == [T0 + timedelta(hours=1), T0 + timedelta(hours=2)]


def test_rows_with_unusable_close_are_skipped_and_counted():
    rows = [candle(0), candle(1, close=None), candle(2, close=0), candle(3, close=float("nan")), candle(4)]
    result, _ = run(FakeClient(pages=[rows]))
    assert len(result.quotes) == 2
    assert result.skipped == 3


def test_interval_ms():
    assert interval_ms("1m") == 60_000
    assert interval_ms("1h") == HOUR
    assert interval_ms("4h") == 4 * HOUR
    assert interval_ms("1d") == 24 * HOUR
    assert interval_ms("1w") == 7 * 24 * HOUR


@pytest.mark.parametrize("bad", ["1M", "h", "0h", "abc", ""])
def test_interval_ms_rejects_unsupported(bad):
    with pytest.raises(HistoryError):
        interval_ms(bad)


# --- paginación ---

def test_paginates_advancing_one_interval_past_last_candle():
    candles = [candle(h) for h in range(10)]
    client = FakeClient(candles=candles)
    result, _ = run(client, page_limit=4)
    assert [c["since"] for c in client.calls[:3]] == [ms(0), ms(4), ms(8)]
    assert len(result.quotes) == 10
    assert result.stop == "empty"


def test_no_infinite_loop_when_exchange_ignores_since():
    # Un exchange que devuelve siempre las mismas velas: sin la regla "no avanza", bucle infinito.
    page = [candle(0), candle(1)]
    client = FakeClient(pages=[page] * 50)
    result, _ = run(client)
    assert len(client.calls) == 2
    assert result.stop == "no_advance"
    assert len(result.quotes) == 2


def test_overlapping_pages_are_deduplicated():
    client = FakeClient(pages=[[candle(0), candle(1), candle(2)], [candle(2), candle(3), candle(4)]])
    result, _ = run(client)
    stamps = [q.timestamp for q in result.quotes]
    assert len(stamps) == len(set(stamps)) == 5
    assert stamps == sorted(stamps)


def test_unsorted_page_is_sorted():
    result, _ = run(FakeClient(pages=[[candle(2), candle(0), candle(1)]]))
    stamps = [q.timestamp for q in result.quotes]
    assert stamps == sorted(stamps)


def test_empty_first_page_stops_with_no_quotes():
    client = FakeClient(pages=[[]])
    result, _ = run(client)
    assert result.quotes == []
    assert result.stop == "empty"
    assert len(client.calls) == 1


def test_empty_page_midway_stops_and_keeps_what_was_fetched():
    client = FakeClient(pages=[[candle(0), candle(1)], [], [candle(5)]])
    result, _ = run(client)
    assert len(result.quotes) == 2
    assert result.stop == "empty"
    assert len(client.calls) == 2


def test_stops_when_until_is_exceeded_and_trims_candles_beyond_it():
    until = T0 + timedelta(hours=3)
    client = FakeClient(candles=[candle(h) for h in range(10)])
    result, _ = run(client, until=until)
    assert result.stop == "until"
    # velas con apertura en [since, until): horas 0, 1, 2
    assert [q.timestamp for q in result.quotes] == [T0 + timedelta(hours=h) for h in (1, 2, 3)]
    assert len(client.calls) == 1


def test_candles_before_since_are_discarded():
    # algunos exchanges devuelven la vela anterior a since
    client = FakeClient(pages=[[candle(-1), candle(0), candle(1)]])
    result, _ = run(client)
    assert len(result.quotes) == 2


def test_hard_request_cap_is_reached_and_flagged():
    # un exchange que avanza una vela por página nunca termina por sí solo
    candles = [candle(h) for h in range(100)]
    client = FakeClient(candles=candles)
    result, _ = run(client, page_limit=1, max_requests=5)
    assert len(client.calls) == 5
    assert result.stop == "max_requests"
    assert result.complete is False
    assert len(result.quotes) == 5


def test_complete_flag_true_when_finished_normally():
    result, _ = run(FakeClient(pages=[[candle(0)], []]))
    assert result.complete is True


def test_retries_count_against_the_request_cap():
    client = FakeClient(pages=[[candle(0)]])
    client.errors = [ccxt.RateLimitExceeded("429")] * 10
    with pytest.raises(HistoryError):
        run(client, max_requests=3)
    assert len(client.calls) == 3


# --- límite de tasa y reintentos ---

def test_sleeps_rate_limit_between_requests_but_not_after_the_last():
    client = FakeClient(candles=[candle(h) for h in range(6)])
    _, sleeps = run(client, page_limit=2)
    # 3 páginas con datos + 1 vacía = 4 peticiones, 3 esperas de rateLimit/1000
    assert len(client.calls) == 4
    assert sleeps == [0.25, 0.25, 0.25]


def test_missing_rate_limit_attribute_falls_back_to_conservative_wait():
    client = FakeClient(candles=[candle(0), candle(1)])
    client.rateLimit = None
    _, sleeps = run(client, page_limit=1)
    assert sleeps and all(s >= 0.25 for s in sleeps)


def test_rate_limit_exceeded_is_retried_with_backoff():
    client = FakeClient(pages=[[candle(0)], []])
    client.errors = [ccxt.RateLimitExceeded("429")]
    result, sleeps = run(client)
    assert len(result.quotes) == 1
    assert sleeps[0] >= 1  # espera de reintento, mayor que el rateLimit normal


def test_network_error_is_retried_then_succeeds():
    client = FakeClient(pages=[[candle(0)], []])
    client.errors = [ccxt.NetworkError("reset")]
    result, _ = run(client)
    assert len(result.quotes) == 1


def test_persistent_network_error_raises_history_error():
    client = FakeClient(pages=[[candle(0)]])
    client.errors = [ccxt.NetworkError("reset")] * 20
    with pytest.raises(HistoryError, match="reset"):
        run(client, max_requests=50)


def test_bad_symbol_from_exchange_raises_symbol_not_found():
    client = FakeClient(pages=[[candle(0)]])
    client.errors = [ccxt.BadSymbol("no existe")]
    with pytest.raises(SymbolNotFound):
        run(client)


def test_symbol_missing_from_loaded_markets_raises_without_any_request():
    client = FakeClient(pages=[[candle(0)]])
    with pytest.raises(SymbolNotFound, match="DOGE/USDT"):
        run(client, symbol="DOGE/USDT")
    assert client.calls == []


def test_other_exchange_error_is_history_error():
    client = FakeClient(pages=[[candle(0)]])
    client.errors = [ccxt.ExchangeError("mantenimiento")]
    with pytest.raises(HistoryError, match="mantenimiento"):
        run(client)


def test_garbage_page_is_history_error():
    with pytest.raises(HistoryError):
        run(FakeClient(pages=["no soy una lista"]))


# --- validación ---

def test_validate_timeframe_accepts_supported():
    validate_timeframe(FakeClient(), "1h")


def test_validate_timeframe_rejects_unsupported_listing_options():
    with pytest.raises(HistoryError, match="1d"):
        validate_timeframe(FakeClient(), "7m")


def test_validate_timeframe_rejects_exchange_without_ohlcv():
    with pytest.raises(HistoryError, match="velas"):
        validate_timeframe(FakeClient(has={"fetchOHLCV": False}), "1h")


def test_validate_timeframe_rejects_exchange_without_timeframes():
    with pytest.raises(HistoryError):
        validate_timeframe(FakeClient(timeframes={}), "1h")


# --- CSV ---

def test_write_quotes_csv_exact_format(tmp_path):
    out = tmp_path / "sub" / "q.csv"
    quotes = [
        Quote("BTC/USDT", Decimal("60000"), T0),
        Quote("BTC/USDT", Decimal("0.000000012345"), T0 + timedelta(hours=1)),
    ]
    write_quotes_csv(out, quotes)
    text = out.read_bytes().decode("utf-8")
    assert text.splitlines() == [
        "timestamp,symbol,price",
        "2026-11-01T00:00:00+00:00,BTC/USDT,60000",
        "2026-11-01T01:00:00+00:00,BTC/USDT,0.000000012345",
    ]


def test_csv_price_never_uses_scientific_notation(tmp_path):
    out = tmp_path / "q.csv"
    write_quotes_csv(out, [Quote("X/USDT", Decimal("1E-8"), T0)])
    row = list(csv.DictReader(out.open(encoding="utf-8")))[0]
    assert row["price"] == "0.00000001"


def test_write_failure_leaves_no_partial_file_and_raises_history_error(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()  # escribir sobre un directorio falla
    with pytest.raises(HistoryError):
        write_quotes_csv(target, [Quote("X/USDT", Decimal(1), T0)])
    assert list(tmp_path.glob("*.tmp")) == []
