from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import backtest.metrics as metrics_module
from backtest.metrics import Metrics, compute_metrics, write_html_report
from core.models import ExitReason, Fill, Market, Position, Side

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def trade(i, pnl, reason=ExitReason.TAKE_PROFIT, day=0, hours=2, fee=Decimal("0")):
    """Una posición cerrada con su fill de compra y de venta (entrada 100, cantidad 1)."""
    opened = T0 + timedelta(days=day)
    closed = opened + timedelta(hours=hours)
    pos_id = f"pos-b{i}"
    pos = Position(pos_id, f"s{i}", "t1", Market.CEX, "BTC/USDT", Decimal("100"), Decimal("1"),
                   Decimal("100"), opened, Decimal("100"), closed, reason, Decimal(100) + pnl)
    buy = Fill(f"fb{i}", f"b{i}", Market.CEX, "BTC/USDT", Side.BUY, Decimal("100"),
               Decimal("1"), fee, opened)
    sell = Fill(f"fs{i}", f"c{i}", Market.CEX, "BTC/USDT", Side.SELL, Decimal(100) + pnl,
                Decimal("1"), fee, closed, position_id=pos_id)
    return pos, [buy, sell]


def open_trade(i):
    opened = T0
    pos = Position(f"pos-b{i}", f"s{i}", "t1", Market.CEX, "BTC/USDT", Decimal("100"),
                   Decimal("1"), Decimal("100"), opened, Decimal("100"))
    buy = Fill(f"fb{i}", f"b{i}", Market.CEX, "BTC/USDT", Side.BUY, Decimal("100"),
               Decimal("1"), Decimal("0"), opened)
    return pos, [buy]


def build(*trades):
    positions = [p for p, _ in trades]
    fills = [f for _, fs in trades for f in fs]
    return fills, positions


def pnls(*values, **kw):
    return build(*[trade(i, Decimal(v), **kw) for i, v in enumerate(values)])


def test_win_rate_counts_closed_positions_only():
    fills, positions = build(
        trade(1, Decimal("50")), trade(2, Decimal("20"), day=1),
        trade(3, Decimal("-30"), day=2), open_trade(4),
    )
    assert compute_metrics(fills, positions).win_rate == Decimal("0.6667")


def test_max_drawdown_measures_peak_to_trough():
    # curva 0 -> +100 -> -50 -> +20 : caída máxima 150
    fills, positions = build(
        trade(1, Decimal("100"), day=0), trade(2, Decimal("-150"), day=1),
        trade(3, Decimal("70"), day=2),
    )
    assert compute_metrics(fills, positions).max_drawdown_usd == Decimal("150")


def test_max_drawdown_counts_a_loss_before_any_gain():
    # el pico es el capital inicial: perder primero ya es caída
    fills, positions = pnls("-40", "10")
    assert compute_metrics(fills, positions).max_drawdown_usd == Decimal("40")


def test_expectancy_is_mean_pnl_per_closed_trade():
    fills, positions = build(
        trade(1, Decimal("30"), day=0), trade(2, Decimal("-10"), day=1),
        trade(3, Decimal("20"), day=2),
    )
    assert compute_metrics(fills, positions).expectancy_usd == Decimal("13.3333")


def test_total_pnl_is_net_of_entry_and_exit_fees():
    fills, positions = build(trade(1, Decimal("50"), fee=Decimal("1.5")))
    assert compute_metrics(fills, positions).total_pnl_usd == Decimal("47")


def test_closes_by_reason_breaks_down_all_reasons():
    # spec §6.3: es la métrica que dice si los umbrales están calibrados
    reasons = [ExitReason.STOP_LOSS, ExitReason.STOP_LOSS, ExitReason.TAKE_PROFIT,
               ExitReason.TIME_STOP, ExitReason.TIME_STOP, ExitReason.TIME_STOP]
    fills, positions = build(*[trade(i, Decimal("1"), reason=r, day=i) for i, r in enumerate(reasons)])
    assert compute_metrics(fills, positions).closes_by_reason == \
        {ExitReason.STOP_LOSS: 2, ExitReason.TAKE_PROFIT: 1, ExitReason.TIME_STOP: 3}


def test_avg_duration_hours_over_closed_positions():
    fills, positions = build(trade(1, Decimal("1"), hours=2), trade(2, Decimal("1"), day=1, hours=4))
    assert compute_metrics(fills, positions).avg_duration_hours == Decimal("3")


def test_sharpe_is_positive_for_a_steady_winner_and_negative_for_a_loser():
    wins = build(*[trade(i, Decimal(10 + i), day=i) for i in range(6)])
    losses = build(*[trade(i, Decimal(-(10 + i)), day=i) for i in range(6)])
    assert compute_metrics(*wins).sharpe > 0
    assert compute_metrics(*losses).sharpe < 0


def test_ratios_are_zero_when_the_series_is_too_short_to_have_a_deviation():
    # una sola jornada: quantstats devuelve NaN y eso no debe llegar al informe
    fills, positions = pnls("10")
    m = compute_metrics(fills, positions)
    assert m.sharpe == Decimal("0") and m.sortino == Decimal("0") and m.volatility == Decimal("0")


def test_closed_position_without_sell_fill_fails_loudly():
    pos, fills = trade(1, Decimal("5"))
    with pytest.raises(ValueError, match="pos-b1"):
        compute_metrics(fills[:1], [pos])


def test_metrics_on_empty_input_do_not_raise():
    m = compute_metrics([], [])
    assert m.total_pnl_usd == Decimal("0") and m.win_rate == Decimal("0") and m.sharpe == Decimal("0")
    assert m.closes_by_reason == {}


def test_empty_input_never_calls_quantstats(monkeypatch):
    class Explodes:
        def __getattr__(self, name):
            raise AssertionError(f"quantstats.{name} llamado con entrada vacía")

    monkeypatch.setattr(metrics_module, "qs", Explodes())
    assert compute_metrics([], []) == Metrics.zero()
    # solo posiciones abiertas tampoco son operaciones cerradas
    fills, positions = build(open_trade(1))
    assert compute_metrics(fills, positions) == Metrics.zero()


def test_html_report_is_written_by_quantstats(tmp_path):
    fills, positions = build(*[trade(i, Decimal((-1) ** i * (10 + i)), day=i) for i in range(8)])
    out = tmp_path / "informe.html"
    write_html_report(fills, positions, out, title="Prueba")
    assert out.exists() and "<html" in out.read_text(encoding="utf-8").lower()


def test_html_report_without_closed_trades_fails_with_a_clear_message(tmp_path):
    with pytest.raises(ValueError, match="cerradas"):
        write_html_report([], [], tmp_path / "x.html")


# --- heurística de quantstats: serie no negativa con máximo > 1 se toma por precios ---

def test_expectancy_is_correct_when_every_trade_wins():
    # quantstats.avg_return daba 0.75 aquí: trataba los USD como precios
    fills, positions = pnls("10", "20", "30")
    assert compute_metrics(fills, positions).expectancy_usd == Decimal("20")


def test_expectancy_is_the_mean_over_all_closed_trades_breakeven_included():
    fills, positions = pnls("10", "20", "0")
    assert compute_metrics(fills, positions).expectancy_usd == Decimal("10")


def test_large_non_negative_returns_are_not_mistaken_for_prices():
    # capital inicial 10 y ganancias grandes: retornos diarios 0.5, 1.333, 0.857 (todos >= 0, uno > 1)
    import numpy as np

    fills, positions = build(*[trade(i, Decimal(p), day=i) for i, p in enumerate((5, 20, 30))])
    m = compute_metrics(fills, positions, starting_equity=Decimal("10"))
    r = np.array([5 / 10, 20 / 15, 30 / 35])
    assert m.max_drawdown_usd == Decimal("0")
    assert m.win_rate == Decimal("1.0000")
    assert m.volatility == Decimal(repr(float(r.std(ddof=1) * np.sqrt(365)))).quantize(Decimal("0.0001"))
    assert m.sharpe == Decimal(repr(float(r.mean() / r.std(ddof=1) * np.sqrt(365)))).quantize(Decimal("0.0001"))
