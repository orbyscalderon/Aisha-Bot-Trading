import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from storage.audit import AuditEvent, AuditLog
from storage.db import init_db

T0 = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)  # microsegundos == 0


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield c
    c.close()


@pytest.fixture
def audit(conn):
    return AuditLog(conn)


def test_record_returns_increasing_ids(audit):
    assert audit.record("signal_rejected", {}) < audit.record("order_sent", {})


def test_payload_roundtrips_as_json(audit):
    audit.record("order_sent", {"amount_usd": "50.00", "symbol": "BTC/USDT"})
    assert audit.events()[0].payload["amount_usd"] == "50.00"


def test_events_filter_by_type(audit):
    audit.record("a", {})
    audit.record("b", {})
    assert [e.event for e in audit.events(event="b")] == ["b"]


def test_update_is_rejected(audit, conn):
    # spec §7.8: solo se inserta, nunca se actualiza ni se borra
    rid = audit.record("x", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET event='y' WHERE id=?", (rid,))


def test_delete_is_rejected(audit, conn):
    rid = audit.record("x", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id=?", (rid,))


def test_rejected_update_and_delete_leave_row_intact(audit, conn):
    rid = audit.record("x", {"k": "v"})
    for sql in ("UPDATE audit_log SET payload='{}' WHERE id=?",
                "DELETE FROM audit_log WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (rid,))
    (ev,) = audit.events()
    assert (ev.id, ev.event, ev.payload) == (rid, "x", {"k": "v"})


def test_bulk_delete_without_where_is_rejected(audit, conn):
    audit.record("x", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")
    assert len(audit.events()) == 1


def test_decimal_and_datetime_payload_survive_as_text(audit):
    audit.record("o", {"amount": Decimal("50.10"), "at": T0})
    p = audit.events()[0].payload
    assert p["amount"] == "50.10"
    assert isinstance(p["at"], str) and p["at"].startswith("2026-11-04")


def test_timestamp_fixed_width_with_zero_microseconds(audit, conn):
    audit.record("a", {}, now=T0)
    audit.record("b", {}, now=T0 + timedelta(microseconds=1))
    raw = [r[0] for r in conn.execute("SELECT timestamp FROM audit_log ORDER BY id")]
    assert raw[0] == "2026-11-04T12:00:00.000000+00:00"
    assert len(raw[0]) == len(raw[1])
    assert raw == sorted(raw)


def test_events_since_filters_inclusively_and_orders(audit):
    audit.record("old", {}, now=T0)
    audit.record("new", {}, now=T0 + timedelta(seconds=1))
    got = audit.events(since=T0 + timedelta(seconds=1))
    assert [e.event for e in got] == ["new"]
    assert [e.event for e in audit.events(since=T0)] == ["old", "new"]


def test_events_returns_frozen_auditevent_with_aware_timestamp(audit):
    audit.record("a", {}, now=T0)
    ev = audit.events()[0]
    assert isinstance(ev, AuditEvent)
    assert ev.timestamp == T0 and ev.timestamp.tzinfo is not None
    with pytest.raises(Exception):
        ev.event = "z"


def test_naive_now_is_rejected(audit):
    with pytest.raises(ValueError):
        audit.record("a", {}, now=datetime(2026, 11, 4))


def test_record_is_committed(tmp_path):
    path = tmp_path / "b.db"
    c1 = init_db(path)
    AuditLog(c1).record("a", {})
    c2 = init_db(path)
    assert len(AuditLog(c2).events()) == 1
    c1.close()
    c2.close()
