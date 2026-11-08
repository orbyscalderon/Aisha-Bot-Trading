"""Tests del comando fetch-quotes. Cliente y temporizador inyectados: sin red ni esperas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ccxt
import pytest
from typer.testing import CliRunner

import cli
from cli import CliDeps, app
from tests.test_cli import write_yaml

UTC = timezone.utc
HOUR = 3_600_000
T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


def row(hour, close):
    return [int((T0 + timedelta(hours=hour)).timestamp() * 1000), 1, 2, 0.5, close, 10]


class FakeExchange:
    rateLimit = 100

    def __init__(self, data=None, timeframes=None, markets=None):
        self.data = data if data is not None else {
            "BTC/USDT": [row(h, 60000 + h) for h in range(6)],
            "ETH/USDT": [row(h, 3000 + h) for h in range(6)],
        }
        self.timeframes = timeframes if timeframes is not None else {"1h": "1h", "1d": "1d"}
        self.markets = markets
        self.has = {"fetchOHLCV": True}
        self.calls = []
        self.failing: dict[str, Exception] = {}

    def load_markets(self):
        if self.markets is None:
            self.markets = {s: {} for s in self.data}
        return self.markets

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(symbol)
        if symbol in self.failing:
            raise self.failing[symbol]
        return [r for r in self.data.get(symbol, []) if r[0] >= since][:limit]


@pytest.fixture
def cfg_path(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


def fetch(cfg_path, tmp_path, *extra, exchange=None, args=None):
    out = tmp_path / "out" / "quotes.csv"
    sleeps = []
    deps = CliDeps(history_client=exchange or FakeExchange(), sleep=sleeps.append, now=lambda: NOW)
    argv = args if args is not None else [
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "BTC/USDT",
        "--since", "2026-11-01T00:00:00+00:00", "--out", str(out), *extra,
    ]
    r = CliRunner().invoke(app, argv, obj=deps)
    return r, out, sleeps


def test_downloads_and_writes_csv_the_backtest_can_read(cfg_path, tmp_path):
    r, out, _ = fetch(cfg_path, tmp_path)
    assert r.exit_code == 0, r.output
    quotes = cli._read_quotes(out)  # el lector real del backtest
    assert len(quotes) == 6
    assert quotes[0].symbol == "BTC/USDT"
    assert quotes[0].price == Decimal("60000")
    assert quotes[0].timestamp == T0 + timedelta(hours=1)  # sello = cierre de la vela


def test_every_output_line_starts_with_mode_prefix(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path)
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert lines and all(ln.startswith("mode: paper") for ln in lines), r.output


def test_reports_count_range_and_file(cfg_path, tmp_path):
    r, out, _ = fetch(cfg_path, tmp_path)
    assert "6 velas" in r.output
    assert "2026-11-01T01:00:00+00:00" in r.output and "2026-11-01T06:00:00+00:00" in r.output
    assert str(out) in r.output


def test_multiple_symbols_share_one_file(cfg_path, tmp_path):
    r, out, _ = fetch(cfg_path, tmp_path, "--symbol", "ETH/USDT")
    assert r.exit_code == 0, r.output
    quotes = cli._read_quotes(out)
    assert {q.symbol for q in quotes} == {"BTC/USDT", "ETH/USDT"}
    assert len(quotes) == 12


def test_repeated_symbol_is_downloaded_once(cfg_path, tmp_path):
    ex = FakeExchange()
    r, out, _ = fetch(cfg_path, tmp_path, "--symbol", "BTC/USDT", exchange=ex)
    assert len(cli._read_quotes(out)) == 6


def test_unknown_symbol_is_reported_and_the_rest_continue(cfg_path, tmp_path):
    r, out, _ = fetch(cfg_path, tmp_path, "--symbol", "NOPE/USDT", "--symbol", "ETH/USDT")
    assert "NOPE/USDT" in r.output and "no existe" in r.output
    assert "Traceback" not in r.output
    assert {q.symbol for q in cli._read_quotes(out)} == {"BTC/USDT", "ETH/USDT"}
    assert r.exit_code == 3  # descarga parcial: distinto de 0 para que un script lo note


def test_all_symbols_unknown_fails_and_writes_nothing(cfg_path, tmp_path):
    r, out, _ = fetch(cfg_path, tmp_path, args=[
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "NOPE/USDT",
        "--since", "2026-11-01T00:00:00+00:00", "--out", str(tmp_path / "q.csv")])
    assert r.exit_code == 1
    assert not (tmp_path / "q.csv").exists()


def test_a_symbol_failing_midway_is_reported_and_others_continue(cfg_path, tmp_path):
    ex = FakeExchange()
    ex.failing["BTC/USDT"] = ccxt.ExchangeError("mantenimiento")
    r, out, _ = fetch(cfg_path, tmp_path, "--symbol", "ETH/USDT", exchange=ex)
    assert "BTC/USDT" in r.output and "mantenimiento" in r.output
    assert {q.symbol for q in cli._read_quotes(out)} == {"ETH/USDT"}
    assert r.exit_code == 3


def test_request_cap_hit_is_called_out_as_truncated(cfg_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "FETCH_MAX_REQUESTS", 2)
    monkeypatch.setattr(cli, "FETCH_PAGE_LIMIT", 1)
    r, out, _ = fetch(cfg_path, tmp_path)
    assert "incompleta" in r.output and "BTC/USDT" in r.output
    assert len(cli._read_quotes(out)) == 2
    assert r.exit_code == 3


def test_unsupported_timeframe_is_a_message_not_a_trace(cfg_path, tmp_path):
    r, out, _ = fetch(cfg_path, tmp_path, "--timeframe", "7m")
    assert r.exit_code == 2
    assert "7m" in r.output and "1h" in r.output
    assert "Traceback" not in r.output and not out.exists()


def test_unknown_exchange_is_a_message(cfg_path, tmp_path):
    out = tmp_path / "q.csv"
    r = CliRunner().invoke(app, [
        "fetch-quotes", "--config", str(cfg_path), "--exchange", "noexiste", "--symbol", "BTC/USDT",
        "--since", "2026-11-01T00:00:00+00:00", "--out", str(out)], obj=CliDeps(now=lambda: NOW))
    assert r.exit_code == 2
    assert "noexiste" in r.output and "Traceback" not in r.output


def test_since_without_timezone_is_rejected(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path, args=[
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "BTC/USDT",
        "--since", "2026-11-01T00:00:00", "--out", str(tmp_path / "q.csv")])
    assert r.exit_code == 2 and "zona horaria" in r.output


def test_until_without_timezone_is_rejected(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path, "--until", "2026-11-01T12:00:00")
    assert r.exit_code == 2 and "zona horaria" in r.output


def test_garbage_date_is_a_message(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path, args=[
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "BTC/USDT",
        "--since", "ayer", "--out", str(tmp_path / "q.csv")])
    assert r.exit_code == 2 and "--since" in r.output and "Traceback" not in r.output


def test_since_not_before_until_is_rejected(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path, "--until", "2026-11-01T00:00:00+00:00")
    assert r.exit_code == 2 and "anterior" in r.output


def test_since_in_the_future_is_rejected(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path, args=[
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "BTC/USDT",
        "--since", "2027-01-01T00:00:00+00:00", "--out", str(tmp_path / "q.csv")])
    assert r.exit_code == 2 and "futuro" in r.output


def test_until_defaults_to_now_and_drops_the_unfinished_candle(cfg_path, tmp_path):
    ex = FakeExchange(data={"BTC/USDT": [row(h, 100 + h) for h in range(24)] + [row(24, 999)]})
    r, out, _ = fetch(cfg_path, tmp_path, exchange=ex)
    quotes = cli._read_quotes(out)
    assert max(q.timestamp for q in quotes) <= NOW
    assert Decimal("999") not in {q.price for q in quotes}


def test_uses_exchange_rate_limit_through_injected_sleep(cfg_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "FETCH_PAGE_LIMIT", 2)
    r, _, sleeps = fetch(cfg_path, tmp_path)
    assert sleeps and set(sleeps) == {0.1}


def test_default_exchange_is_the_one_the_rest_of_the_cli_uses(cfg_path, tmp_path, monkeypatch):
    seen = []
    ex = FakeExchange()
    monkeypatch.setattr(cli, "make_history_client", lambda exchange_id: seen.append(exchange_id) or ex)
    out = tmp_path / "q.csv"
    r = CliRunner().invoke(app, [
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "BTC/USDT",
        "--since", "2026-11-01T00:00:00+00:00", "--out", str(out)],
        obj=CliDeps(sleep=lambda s: None, now=lambda: NOW))
    assert r.exit_code == 0, r.output
    assert seen == [cli.DEFAULT_EXCHANGE]


def test_missing_symbol_option_is_a_usage_error(cfg_path, tmp_path):
    r, _, _ = fetch(cfg_path, tmp_path, args=[
        "fetch-quotes", "--config", str(cfg_path),
        "--since", "2026-11-01T00:00:00+00:00", "--out", str(tmp_path / "q.csv")])
    assert r.exit_code != 0


def test_unwritable_output_is_a_message(cfg_path, tmp_path):
    bad = tmp_path / "dir"
    bad.mkdir()
    r, _, _ = fetch(cfg_path, tmp_path, args=[
        "fetch-quotes", "--config", str(cfg_path), "--symbol", "BTC/USDT",
        "--since", "2026-11-01T00:00:00+00:00", "--out", str(bad)])
    assert r.exit_code == 1 and "no se pudo escribir" in r.output and "Traceback" not in r.output


def test_market_load_failure_is_a_message(cfg_path, tmp_path):
    ex = FakeExchange()
    ex.load_markets = lambda: (_ for _ in ()).throw(ccxt.NetworkError("sin red"))
    r, _, _ = fetch(cfg_path, tmp_path, exchange=ex)
    assert r.exit_code == 1 and "sin red" in r.output and "Traceback" not in r.output
