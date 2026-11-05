from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.runner import BacktestResult, HistoricalSource, run_backtest
from core.config import BotConfig
from core.engine import Engine
from core.models import ExitReason, Market, Quote, Side, Signal
from ports.market import MarketDataSource, QuoteUnavailable
from ports.signal_source import SignalSource

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
STEP = timedelta(hours=1)
END = T0 + timedelta(hours=3)


def cfg(**exits):
    return BotConfig.model_validate({
        "mode": "paper",
        "sizing": {"strategy": "flat", "flat_amount_usd": 100},
        "exits": {"primary": "copy_sell", **exits},
        "risk": {"max_usd_per_token": 1000, "max_open_positions": 5, "max_usd_per_day": 5000,
                 "max_daily_loss_usd": 500, "hard_max_order_usd": 1000},
        "cadence": {"entry_cron": "0 * * * *", "exit_interval_seconds": 60},
    })


def sig_at(ts, id_="s1", symbol="BTC/USDT"):
    return Signal(id_, "t1", Market.CEX, symbol, Side.BUY, ts, None)


def quotes_from(*prices, symbol="BTC/USDT"):
    return [Quote(symbol, Decimal(p), T0 + i * STEP) for i, p in enumerate(prices)]


def falling_quotes():
    return quotes_from(100, 100, 70, 70)


def test_backtest_uses_the_same_engine_class():
    # spec §6.1: una divergencia entre backtest y vivo sería un defecto
    r = run_backtest(cfg(stop_loss_pct=20), [sig_at(T0)], falling_quotes(), T0, END, STEP)
    assert r.engine_class is Engine


def test_backtest_is_deterministic():
    args = (cfg(stop_loss_pct=20), [sig_at(T0)], falling_quotes(), T0, END, STEP)
    assert run_backtest(*args).metrics == run_backtest(*args).metrics


def test_backtest_opens_and_closes_across_time_steps():
    r = run_backtest(cfg(stop_loss_pct=20), [sig_at(T0)], falling_quotes(), T0, END, STEP)
    assert r.metrics.closes_by_reason[ExitReason.STOP_LOSS] == 1
    (pos,) = r.positions
    assert pos.opened_at == T0 and pos.closed_at == T0 + 2 * STEP
    # deslizamiento 10 bps en contra, en ambas patas: se entra más caro y se sale más barato
    assert pos.entry_price == Decimal("100") * Decimal("1.001")
    assert [f.side for f in r.fills] == [Side.BUY, Side.SELL]
    assert r.metrics.total_pnl_usd < 0


def test_a_signal_is_copied_once_however_many_steps_follow():
    r = run_backtest(cfg(), [sig_at(T0)], quotes_from(100, 100, 100, 100), T0, END, STEP)
    assert len(r.positions) == 1 and len(r.fills) == 1


def test_signals_from_the_future_are_not_copied_early():
    later = T0 + 2 * STEP
    r = run_backtest(cfg(), [sig_at(later)], quotes_from(100, 100, 100, 100), T0, END, STEP)
    assert [p.opened_at for p in r.positions] == [later]


def test_signals_before_start_are_not_copied():
    r = run_backtest(cfg(), [sig_at(T0 - STEP)], quotes_from(100, 100, 100, 100), T0, END, STEP)
    assert r.positions == []


def test_time_stop_closes_by_elapsed_simulated_time():
    r = run_backtest(cfg(time_stop_hours=1), [sig_at(T0)], quotes_from(100, 100, 100, 100),
                     T0, END, STEP)
    assert r.metrics.closes_by_reason == {ExitReason.TIME_STOP: 1}


def test_backtest_without_signals_has_zero_metrics():
    r = run_backtest(cfg(), [], quotes_from(100, 100), T0, END, STEP)
    assert r.metrics.total_pnl_usd == Decimal("0") and r.positions == [] and r.fills == []


def test_result_is_frozen():
    r = run_backtest(cfg(), [], [], T0, END, STEP)
    assert isinstance(r, BacktestResult)
    with pytest.raises(AttributeError):
        r.metrics = None  # type: ignore[misc]


def test_a_stop_file_in_the_working_directory_does_not_affect_the_backtest(tmp_path, monkeypatch):
    # el backtest no debe heredar el kill switch del proceso real
    monkeypatch.chdir(tmp_path)
    (tmp_path / "STOP").write_text("x")
    r = run_backtest(cfg(), [sig_at(T0)], quotes_from(100, 100, 100, 100), T0, END, STEP)
    assert len(r.positions) == 1


@pytest.mark.parametrize("start,end,step", [
    (T0, END, timedelta(0)), (T0, T0 - STEP, STEP),
    (T0.replace(tzinfo=None), END, STEP),
])
def test_invalid_time_arguments_are_rejected(start, end, step):
    with pytest.raises(ValueError):
        run_backtest(cfg(), [], [], start, end, step)


def test_mixed_markets_are_rejected_because_the_executor_models_one_market():
    other = Signal("s2", "t1", Market.SOLANA, "SOL/USDC", Side.BUY, T0, None)
    with pytest.raises(ValueError, match="mercado"):
        run_backtest(cfg(), [sig_at(T0), other], [], T0, END, STEP)


def test_report_can_be_written_from_a_result(tmp_path):
    r = run_backtest(cfg(stop_loss_pct=20), [sig_at(T0)], falling_quotes(), T0, END, STEP)
    out = tmp_path / "r.html"
    r.write_report(out)
    assert out.exists()


# --- HistoricalSource ---

def test_historical_source_implements_both_read_ports():
    src = HistoricalSource([], [])
    assert isinstance(src, SignalSource) and isinstance(src, MarketDataSource)


def test_quote_is_the_latest_not_after_the_clock():
    src = HistoricalSource([], quotes_from(100, 110, 120))
    src.set_time(T0 + STEP + timedelta(minutes=30))
    assert src.get_quote("BTC/USDT").price == Decimal("110")


def test_quote_in_the_future_is_unavailable_never_leaked():
    src = HistoricalSource([], quotes_from(100, 110))
    src.set_time(T0 - timedelta(minutes=1))
    with pytest.raises(QuoteUnavailable):
        src.get_quote("BTC/USDT")
    with pytest.raises(QuoteUnavailable):
        src.get_quote("ETH/USDT")


def test_using_the_source_before_setting_the_clock_fails():
    with pytest.raises(RuntimeError):
        HistoricalSource([], quotes_from(100)).get_quote("BTC/USDT")


def test_list_followed_is_the_sorted_set_of_traders():
    s = [Signal("a", "t2", Market.CEX, "X", Side.BUY, T0, None),
         Signal("b", "t1", Market.CEX, "X", Side.BUY, T0, None),
         Signal("c", "t2", Market.CEX, "X", Side.BUY, T0, None)]
    assert HistoricalSource(s, []).list_followed() == ["t1", "t2"]


# --- el capital que ve el motor es el real, no una constante ---

def protected_cfg(**protections):
    raw = cfg().model_dump(mode="json")
    raw["sizing"]["flat_amount_usd"] = 1000
    raw["protections"] = protections
    return BotConfig.model_validate(raw)


def two_symbol_run(protections):
    # BTC se hunde un 50 % (latente -500 sobre 10000); luego llega una señal de ETH
    sigs = [sig_at(T0), sig_at(T0 + 2 * STEP, id_="s2", symbol="ETH/USDT")]
    quotes = quotes_from(100, 100, 50, 50) + quotes_from(100, 100, 100, 100, symbol="ETH/USDT")
    return run_backtest(protected_cfg(**protections), sigs, quotes, T0, END, STEP)


def test_drawdown_protection_fires_in_backtest_because_balance_tracks_losses():
    # con un capital plano esta protección no podía dispararse nunca en backtest
    assert len(two_symbol_run({}).positions) == 2
    assert [p.symbol for p in two_symbol_run({"max_drawdown_pct": 3}).positions] == ["BTC/USDT"]


def test_balance_is_initial_plus_realized_and_unrealized_pnl():
    from adapters.paper.executor import PaperExecutor
    from core.models import Order
    from storage.db import init_db
    from storage.repository import Repository

    src = HistoricalSource([], quotes_from(100, 100, 50), balance=Decimal("10000"))
    src.set_time(T0)
    conn = init_db(":memory:")
    repo = Repository(conn)
    src.bind_repository(repo)
    assert src.get_balance() == Decimal("10000")
    ex = PaperExecutor(src, 0, 10, 8)
    fill = ex.submit_buy(Order("c1", Market.CEX, "BTC/USDT", Side.BUY, Decimal("1000"), Decimal("100")))
    from core.models import Position
    repo.save_position(Position("pos-c1", "s", "t1", Market.CEX, "BTC/USDT", fill.price, fill.quantity,
                                Decimal("1000"), T0, fill.price))
    repo.save_fill(fill)
    # a 100: capital intacto menos la comisión de entrada (1 USD)
    assert src.get_balance() == Decimal("10000") - Decimal("1")
    src.set_time(T0 + 2 * STEP)  # BTC a 50: latente -500
    assert src.get_balance() == Decimal("10000") - Decimal("1") - Decimal("500")
    conn.close()


# --- posiciones abiertas al final: no pueden esconderse ---

def test_result_reports_positions_still_open_and_their_unrealized_pnl():
    r = run_backtest(cfg(), [sig_at(T0)], falling_quotes(), T0, END, STEP)  # copy_sell: nunca cierra
    assert r.open_positions_count == 1
    (pos,) = r.positions
    assert r.unrealized_pnl_usd == (Decimal("70") - pos.entry_price) * pos.quantity
    assert r.unrealized_pnl_usd < 0
    assert r.metrics.closes_by_reason == {}  # lo realizado parece sano; el resultado no lo oculta


def test_result_without_open_positions_reports_zero():
    r = run_backtest(cfg(stop_loss_pct=20), [sig_at(T0)], falling_quotes(), T0, END, STEP)
    assert r.open_positions_count == 0 and r.unrealized_pnl_usd == Decimal("0")


def test_report_shows_open_positions(tmp_path):
    # una posición cerrada (para que haya informe) y una abierta
    sigs = [sig_at(T0), sig_at(T0, id_="s2", symbol="ETH/USDT")]
    quotes = falling_quotes() + quotes_from(100, 100, 100, 100, symbol="ETH/USDT")
    raw = cfg(stop_loss_pct=20).model_dump(mode="json")
    r = run_backtest(BotConfig.model_validate(raw), sigs, quotes, T0, END, STEP)
    assert r.open_positions_count == 1
    out = tmp_path / "r.html"
    r.write_report(out)
    text = out.read_text(encoding="utf-8")
    assert "Posiciones abiertas al final: 1" in text


def test_balance_raises_when_an_open_position_has_no_quote():
    # decisión, no accidente: igual que el adaptador real, un hueco en los datos
    # no se valora en silencio (el motor registra el error del ciclo y sigue).
    from core.models import Position
    from storage.db import init_db
    from storage.repository import Repository

    src = HistoricalSource([], quotes_from(100))
    src.set_time(T0)
    conn = init_db(":memory:")
    repo = Repository(conn)
    src.bind_repository(repo)
    repo.save_position(Position("p1", "s", "t1", Market.CEX, "ETH/USDT", Decimal("1"), Decimal("1"),
                                Decimal("1"), T0, Decimal("1")))
    with pytest.raises(QuoteUnavailable):
        src.get_balance()
    conn.close()

