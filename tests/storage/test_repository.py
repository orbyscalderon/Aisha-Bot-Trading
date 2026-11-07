import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.models import ExitReason, Fill, Market, Position, Side
from storage.db import init_db
from storage.repository import Repository

T0 = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 4, 13, 30, 15, 123456, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield c
    c.close()


@pytest.fixture
def repo(conn):
    return Repository(conn)


def pos_with(id="p1", **kw):
    base = dict(
        id=id, signal_id="s-" + id, trader_id="t1", market=Market.CEX, symbol="BTC",
        entry_price=Decimal("100.5"), quantity=Decimal("2"), amount_usd=Decimal("201"),
        opened_at=T0, peak_price=Decimal("100.5"),
    )
    base.update(kw)
    return Position(**base)


def fill_with(id="f1", ts=T0, **kw):
    base = dict(
        id=id, client_order_id="ct-" + id, market=Market.CEX, symbol="BTC", side=Side.BUY,
        price=Decimal("100.5"), quantity=Decimal("2"), fee_usd=Decimal("0.0201"), timestamp=ts,
    )
    base.update(kw)
    return Fill(**base)


def test_init_db_is_idempotent(tmp_path):
    p = tmp_path / "x.db"
    init_db(p).close()
    init_db(p).close()


def test_init_db_creates_six_tables_wal_and_fk(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"positions", "fills", "cursors", "frozen_allowlist", "order_intents", "audit_log"} <= names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_save_and_read_position_preserves_decimal_precision(repo):
    q = Decimal("0.000000012345")
    repo.save_position(pos_with(quantity=q, entry_price=Decimal("123456789.123456789012")))
    got = repo.get_position("p1")
    assert got.quantity == q and got.quantity.as_tuple() == q.as_tuple()
    assert got.entry_price == Decimal("123456789.123456789012")
    assert isinstance(got.quantity, Decimal)


def test_decimals_stored_as_text_not_real(conn, repo):
    # texto plano, sin notacion cientifica
    repo.save_position(pos_with(quantity=Decimal("0.000000012345")))
    t = conn.execute("SELECT typeof(quantity), typeof(entry_price) FROM positions").fetchone()
    assert tuple(t) == ("text", "text")
    assert conn.execute("SELECT quantity FROM positions").fetchone()[0] == "0.000000012345"


def test_position_roundtrip_is_equal_and_utc(repo):
    p = pos_with(opened_at=T1)
    repo.save_position(p)
    got = repo.get_position("p1")
    assert got == p
    assert got.opened_at.tzinfo is timezone.utc
    assert got.market is Market.CEX


def test_get_position_missing_returns_none(repo):
    assert repo.get_position("nope") is None


def test_naive_datetime_is_rejected(repo):
    with pytest.raises(ValueError):
        repo.save_position(pos_with(opened_at=datetime(2026, 11, 4, 12, 0)))


def test_non_utc_aware_datetime_is_normalised_to_utc(repo):
    tz = timezone(timedelta(hours=-5))
    repo.save_position(pos_with(opened_at=datetime(2026, 11, 4, 7, 0, tzinfo=tz)))
    got = repo.get_position("p1").opened_at
    assert got == T0 and got.tzinfo is timezone.utc


def test_save_position_duplicate_id_raises(repo):
    repo.save_position(pos_with())
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_position(pos_with())


def test_open_positions_excludes_closed(repo):
    a, b = pos_with("a"), pos_with("b")
    repo.save_position(a)
    repo.save_position(b)
    repo.close_position(b.id, Decimal("90"), ExitReason.STOP_LOSS, T1)
    assert [p.id for p in repo.open_positions()] == [a.id]


def test_close_position_persists_exit_fields(repo):
    repo.save_position(pos_with())
    repo.close_position("p1", Decimal("90.000000001"), ExitReason.STOP_LOSS, T1)
    got = repo.get_position("p1")
    assert got.exit_price == Decimal("90.000000001")
    assert got.exit_reason is ExitReason.STOP_LOSS
    assert got.closed_at == T1 and got.closed_at.tzinfo is timezone.utc


def test_close_position_twice_or_missing_raises(repo):
    repo.save_position(pos_with())
    repo.close_position("p1", Decimal("90"), ExitReason.MANUAL, T1)
    with pytest.raises(ValueError):
        repo.close_position("p1", Decimal("80"), ExitReason.MANUAL, T1)
    assert repo.get_position("p1").exit_price == Decimal("90")
    with pytest.raises(ValueError):
        repo.close_position("nope", Decimal("1"), ExitReason.MANUAL, T1)


def test_update_peak_price(repo):
    repo.save_position(pos_with())
    repo.update_peak_price("p1", Decimal("123.000000007"))
    assert repo.get_position("p1").peak_price == Decimal("123.000000007")
    with pytest.raises(ValueError):
        repo.update_peak_price("nope", Decimal("1"))


def test_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "restart.db"
    c1 = init_db(path)
    r1 = Repository(c1)
    r1.save_position(pos_with("a"))
    r1.save_position(pos_with("b"))
    r1.close_position("b", Decimal("90"), ExitReason.COPY_SELL, T1)
    r1.advance_cursor("cex", T1)
    r1.freeze_allowlist("s1", {"t1"})
    c1.close()  # simula el reinicio del proceso
    c2 = init_db(path)
    r2 = Repository(c2)
    assert [p.id for p in r2.open_positions()] == ["a"]
    assert r2.get_cursor("cex") == T1
    assert r2.get_frozen_allowlist("s1") == frozenset({"t1"})
    c2.close()


def test_save_fill_roundtrip_and_between(repo):
    f1 = fill_with("f1", T0)
    f2 = fill_with("f2", T1, quantity=Decimal("0.000000012345"))
    f3 = fill_with("f3", T0 + timedelta(days=2))
    for f in (f3, f2, f1):
        repo.save_fill(f)
    got = repo.fills_between(T0, T0 + timedelta(days=1))
    assert got == [f1, f2]  # ordenados por tiempo, [start, end)
    assert got[1].quantity == Decimal("0.000000012345")
    assert repo.fills_between(T0, T1) == [f1]  # end exclusivo


def test_fills_between_orders_correctly_with_and_without_microseconds(repo):
    whole = datetime(2026, 11, 4, 12, 0, 1, tzinfo=timezone.utc)
    micro = datetime(2026, 11, 4, 12, 0, 0, 500000, tzinfo=timezone.utc)
    repo.save_fill(fill_with("a", whole))
    repo.save_fill(fill_with("b", micro))
    assert [f.id for f in repo.fills_between(T0, T0 + timedelta(hours=1))] == ["b", "a"]


def test_fill_with_position_id_and_foreign_key(repo):
    repo.save_position(pos_with())
    f = fill_with("fs", side=Side.SELL, position_id="p1")
    repo.save_fill(f)
    assert repo.fills_between(T0, T0 + timedelta(days=1))[0].position_id == "p1"
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_fill(fill_with("fx", position_id="ghost"))


def test_cursor_absent_returns_none(repo):
    assert repo.get_cursor("cex") is None


def test_advance_cursor_roundtrip_is_utc(repo):
    repo.advance_cursor("cex", T1)
    assert repo.get_cursor("cex") == T1
    assert repo.get_cursor("cex").tzinfo is timezone.utc


def test_advance_cursor_overwrites_and_is_per_source(repo):
    repo.advance_cursor("cex", T0)
    repo.advance_cursor("cex", T1)
    repo.advance_cursor("fomo", T0)
    assert repo.get_cursor("cex") == T1
    assert repo.get_cursor("fomo") == T0


def test_frozen_allowlist_absent_returns_none(repo):
    assert repo.get_frozen_allowlist("s1") is None


def test_freeze_allowlist_roundtrip(repo):
    repo.freeze_allowlist("s1", {"t2", "t1"})
    got = repo.get_frozen_allowlist("s1")
    assert got == frozenset({"t1", "t2"}) and isinstance(got, frozenset)


def test_freeze_empty_allowlist_is_distinct_from_absent(repo):
    repo.freeze_allowlist("s1", set())
    assert repo.get_frozen_allowlist("s1") == frozenset()


def test_freeze_allowlist_is_write_once_per_session(repo):
    repo.freeze_allowlist("s1", {"t1"})
    with pytest.raises(ValueError):
        repo.freeze_allowlist("s1", {"t1", "t2"})
    assert repo.get_frozen_allowlist("s1") == frozenset({"t1"})


def test_order_intents_shape(conn):
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(order_intents)")}
    assert list(cols) == ["client_order_id", "status", "payload", "created_at", "error"]
    assert cols["client_order_id"][5] == 1  # pk
    assert cols["error"][3] == 0  # nullable


def test_order_intents_rejects_duplicate_primary_key(conn):
    conn.execute("INSERT INTO order_intents VALUES ('ct-a','RESERVED','{}','2026-11-04',NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_intents VALUES ('ct-a','RESERVED','{}','2026-11-04',NULL)")


def test_audit_log_shape(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(audit_log)")]
    assert cols == ["id", "timestamp", "event", "payload"]
    conn.execute("INSERT INTO audit_log (timestamp,event,payload) VALUES ('t','e','{}')")
    conn.execute("INSERT INTO audit_log (timestamp,event,payload) VALUES ('t','e','{}')")
    assert [r[0] for r in conn.execute("SELECT id FROM audit_log")] == [1, 2]


def test_audit_log_rejects_update_and_delete(conn):
    conn.execute("INSERT INTO audit_log (timestamp,event,payload) VALUES ('t','e','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET event='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")
    assert conn.execute("SELECT event FROM audit_log").fetchone()[0] == "e"


def test_closed_positions_since_filters_by_close_time(repo):
    for id in ("a", "b", "c"):
        repo.save_position(pos_with(id))
    repo.close_position("a", Decimal("90"), ExitReason.STOP_LOSS, T0)
    repo.close_position("b", Decimal("90"), ExitReason.STOP_LOSS, T1)
    # "c" sigue abierta
    assert [p.id for p in repo.closed_positions_since(T1)] == ["b"]
    assert [p.id for p in repo.closed_positions_since(T0)] == ["a", "b"]


def test_state_roundtrip_and_missing_key(repo):
    assert repo.get_state("peak_equity_usd") is None
    repo.set_state("peak_equity_usd", "1234.5")
    repo.set_state("peak_equity_usd", "2000")  # sobrescribe
    assert repo.get_state("peak_equity_usd") == "2000"


def test_state_survives_closing_and_reopening_the_database(tmp_path):
    c = init_db(tmp_path / "x.db")
    Repository(c).set_state("k", "v")
    c.close()
    c2 = init_db(tmp_path / "x.db")
    assert Repository(c2).get_state("k") == "v"
    c2.close()
