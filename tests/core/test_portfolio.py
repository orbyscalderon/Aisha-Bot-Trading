from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from core.models import Fill, Market, Position, Quote, Side
from core.portfolio import Portfolio
from storage.db import init_db
from storage.repository import Repository

UTC = timezone.utc
D = date(2026, 11, 4)


@pytest.fixture
def repo(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield Repository(c)
    c.close()


_n = 0


def pos(symbol="BTC/USDT", amount=200, entry=100, qty=None, id=None, closed=False):
    global _n
    _n += 1
    qty = Decimal(qty) if qty is not None else Decimal(amount) / Decimal(entry)
    return Position(
        id=id or f"p{_n}", signal_id=f"s{_n}", trader_id="t1", market=Market.CEX, symbol=symbol,
        entry_price=Decimal(entry), quantity=qty, amount_usd=Decimal(amount),
        opened_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC), peak_price=Decimal(entry),
        closed_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC) if closed else None,
    )


def q(price, symbol="BTC/USDT"):
    return Quote(symbol=symbol, price=Decimal(price), timestamp=datetime(2026, 11, 4, 10, 0, tzinfo=UTC))


def fill(ts, side, price, qty, position_id=None, fee=0, id=None):
    return Fill(
        id=id or f"f{ts.isoformat()}{side.value}", client_order_id=f"c{ts.isoformat()}{side.value}",
        market=Market.CEX, symbol="BTC/USDT", side=side, price=Decimal(price),
        quantity=Decimal(qty), fee_usd=Decimal(fee), timestamp=ts, position_id=position_id,
    )


def repo_with(repo, *positions):
    for p in positions:
        repo.save_position(p)
    return repo


def test_exposure_aggregates_positions_in_same_symbol(repo):
    repo_with(repo, pos("BTC/USDT", 100), pos("BTC/USDT", 50), pos("ETH/USDT", 70))
    assert Portfolio(repo).exposure_by_symbol() == {
        "BTC/USDT": Decimal("150"), "ETH/USDT": Decimal("70")}


def test_exposure_ignores_closed_positions(repo):
    repo_with(repo, pos("BTC/USDT", 100), pos("BTC/USDT", 50, closed=True))
    assert Portfolio(repo).exposure_by_symbol() == {"BTC/USDT": Decimal("100")}


def test_open_position_count(repo):
    repo_with(repo, pos(), pos(), pos(closed=True))
    assert Portfolio(repo).open_position_count() == 2


def test_position_for_symbol(repo):
    p = pos("BTC/USDT")
    repo_with(repo, p)
    assert Portfolio(repo).position_for_symbol("BTC/USDT") == p
    assert Portfolio(repo).position_for_symbol("ETH/USDT") is None


def test_unrealized_pnl_sums_open_positions(repo):
    # entrada 100 x2 uds, precio 110 -> +20
    repo_with(repo, pos(entry=100, qty=2))
    assert Portfolio(repo).unrealized_pnl_usd({"BTC/USDT": q(110)}) == Decimal("20")


def test_unrealized_pnl_skips_symbol_without_quote(repo):
    repo_with(repo, pos("BTC/USDT", entry=100, qty=2), pos("MISSING", entry=100, qty=2))
    assert Portfolio(repo).unrealized_pnl_usd({"BTC/USDT": q(110)}) == Decimal("20")


def test_portfolio_holds_no_state_and_reads_repo_each_call(repo):
    p = Portfolio(repo)
    assert p.open_position_count() == 0
    repo_with(repo, pos())
    assert p.open_position_count() == 1


def test_realized_pnl_counts_only_requested_day(repo):
    repo_with(repo, pos(id="p1", entry=100, qty=2))
    repo.save_fill(fill(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), Side.SELL, 115, 2, "p1"))
    repo.save_fill(fill(datetime(2026, 11, 5, 10, 0, tzinfo=UTC), Side.SELL, 200, 2, "p1", id="f2"))
    assert Portfolio(repo).realized_pnl_usd(D) == Decimal("30")


def test_realized_pnl_includes_fees(repo):
    # venta con +50 de PnL bruto y 5 de comisión
    repo_with(repo, pos(id="p1", entry=100, qty=2))
    repo.save_fill(fill(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), Side.SELL, 125, 2, "p1", fee=5))
    assert Portfolio(repo).realized_pnl_usd(D) == Decimal("45")


def test_realized_pnl_subtracts_buy_fees_too(repo):
    repo.save_fill(fill(datetime(2026, 11, 4, 9, 0, tzinfo=UTC), Side.BUY, 100, 2, fee=1))
    assert Portfolio(repo).realized_pnl_usd(D) == Decimal("-1")


def test_realized_pnl_sell_without_position_fails_loudly(repo):
    repo.save_fill(fill(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), Side.SELL, 60, 1))
    with pytest.raises(ValueError, match="unknown position"):
        Portfolio(repo).realized_pnl_usd(D)


def test_spent_counts_only_buys_of_the_day_half_open(repo):
    repo.save_fill(fill(datetime(2026, 11, 4, 0, 0, tzinfo=UTC), Side.BUY, 100, "0.1", id="a"))
    repo.save_fill(fill(datetime(2026, 11, 4, 23, 59, tzinfo=UTC), Side.BUY, 100, "0.2", id="b"))
    repo.save_fill(fill(datetime(2026, 11, 5, 0, 0, tzinfo=UTC), Side.BUY, 100, "9", id="c"))
    assert Portfolio(repo).spent_usd(D) == Decimal("30")


def test_amounts_are_decimal(repo):
    p = Portfolio(repo)
    assert isinstance(p.spent_usd(D), Decimal) and isinstance(p.realized_pnl_usd(D), Decimal)
    assert isinstance(p.unrealized_pnl_usd({}), Decimal)


def test_realized_pnl_buy_and_sell_with_all_fees(repo):
    # compra 2 a 100 (fee 0), vende 2 a 60: pierde 80, más 1 de comisión; gasto solo compras
    repo_with(repo, pos(id="p1", entry=100, qty=2))
    repo.save_fill(fill(datetime(2026, 11, 4, 9, 0, tzinfo=UTC), Side.BUY, 100, 2, id="b"))
    repo.save_fill(fill(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), Side.SELL, 60, 2, "p1", fee=1))
    assert Portfolio(repo).realized_pnl_usd(D) == Decimal("-81")
    assert Portfolio(repo).spent_usd(D) == Decimal("200")


# --- patrimonio total: única implementación (la usan el modo simulado y el backtest) ---

def test_total_equity_is_initial_plus_cash_flow_plus_mark_to_market(repo):
    from ports.market import QuoteUnavailable  # noqa: F401
    p1 = pos(amount=200, entry=100, qty="2")
    repo_with(repo, p1)
    repo.save_fill(fill(datetime(2026, 11, 4, 9, 0, tzinfo=UTC), Side.BUY, 100, 2, fee="1"))
    equity = Portfolio(repo).total_equity_usd(lambda s: q(120, s), Decimal(1000))
    # 1000 - 200 (compra) - 1 (comisión) + 2 x 120 (valor a mercado)
    assert equity == Decimal(1039)


def test_total_equity_skips_unquotable_position_with_a_warning(repo, caplog):
    # Política única: una posición sin cotización NO congela el capital (y con él todas las
    # entradas); se omite su valoración y se avisa. Igual que el adaptador de CEX.
    from ports.market import QuoteUnavailable
    ok = pos("BTC/USDT", amount=200, entry=100, qty="2")
    gone = pos("DEAD/USDT", amount=100, entry=10, qty="10")
    repo_with(repo, ok, gone)

    def quote(symbol):
        if symbol == "DEAD/USDT":
            raise QuoteUnavailable(symbol)
        return q(120, symbol)

    with caplog.at_level("WARNING"):
        equity = Portfolio(repo).total_equity_usd(quote, Decimal(1000))
    assert equity == Decimal(1240)  # 1000 + 2 x 120; DEAD fuera
    assert "DEAD/USDT" in caplog.text
