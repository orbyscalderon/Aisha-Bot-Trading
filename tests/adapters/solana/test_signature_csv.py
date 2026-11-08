"""La firma de la transaccion viaja en el CSV y fija el id de la senal al leerlo."""

from __future__ import annotations

from adapters.cex.source import CexCsvSignalSource
from adapters.solana.source import parse_swap, write_signals_csv
from tests.adapters.solana.test_source import T0, WALLET, WALLET2, buy, source



def test_csv_carries_the_signature_and_the_backtest_reader_uses_it_for_the_id(tmp_path):
    s = parse_swap(buy("sigA"), WALLET)
    out = tmp_path / "s.csv"
    write_signals_csv(out, [s], {s.id: "sigA"})
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "timestamp,trader_id,symbol,side,amount_usd,signature"
    assert lines[1].endswith(",sigA")
    assert CexCsvSignalSource(out).fetch_trades(None)[0].id == s.id  # mismo id que en vivo


def test_two_identical_looking_swaps_keep_distinct_ids_through_the_csv(tmp_path):
    a, b = parse_swap(buy("sigA"), WALLET), parse_swap(buy("sigB"), WALLET)  # misma hora e importe
    out = tmp_path / "s.csv"
    write_signals_csv(out, [a, b], {a.id: "sigA", b.id: "sigB"})
    ids = [x.id for x in CexCsvSignalSource(out).fetch_trades(None)]
    assert len(set(ids)) == 2


def test_csv_without_signature_column_or_with_blank_one_keeps_the_row_hash(tmp_path):
    import hashlib

    row = "2026-10-02T12:00:00+00:00,w,BTC/USDT,buy,250"
    old = tmp_path / "old.csv"
    old.write_text("timestamp,trader_id,symbol,side,amount_usd\n" + row + "\n", encoding="utf-8")
    new = tmp_path / "new.csv"
    new.write_text("timestamp,trader_id,symbol,side,amount_usd,signature\n" + row + ",\n", encoding="utf-8")
    old_id = CexCsvSignalSource(old).fetch_trades(None)[0].id
    assert old_id == CexCsvSignalSource(new).fetch_trades(None)[0].id
    assert old_id == hashlib.sha256("\x1f".join(row.split(",")).encode("utf-8")).hexdigest()[:32]


def test_source_exposes_signatures_for_the_csv():
    src = source({WALLET: [buy("w1", T0 + 20)], WALLET2: []})
    signals = src.fetch_trades(None)
    assert src.last_signatures[signals[0].id] == "w1"
