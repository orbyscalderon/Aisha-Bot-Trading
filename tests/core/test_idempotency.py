import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.idempotency import IntentStatus, OrderIntent, client_order_id
from core.models import Fill, Market, Side
from storage.db import init_db
from storage.repository import Repository

T0 = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
PAYLOAD = {"symbol": "BTC/USDT", "side": "buy", "amount_usd": "100"}


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield c
    c.close()


@pytest.fixture
def repo(conn):
    return Repository(conn)


def fill_for(coid):
    return Fill(
        id="f-" + coid, client_order_id=coid, market=Market.CEX, symbol="BTC/USDT",
        side=Side.BUY, price=Decimal("100"), quantity=Decimal("1"),
        fee_usd=Decimal("0.1"), timestamp=T0,
    )


# --- client_order_id ----------------------------------------------------

def test_client_order_id_is_deterministic():
    assert client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0) == \
           client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0)


def test_client_order_id_differs_per_field():
    base = client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0)
    assert base != client_order_id("s2", "t1", "BTC/USDT", Side.BUY, T0)
    assert base != client_order_id("s1", "t2", "BTC/USDT", Side.BUY, T0)
    assert base != client_order_id("s1", "t1", "ETH/USDT", Side.BUY, T0)
    assert base != client_order_id("s1", "t1", "BTC/USDT", Side.SELL, T0)
    assert base != client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T1)


def test_client_order_id_fits_exchange_limit():
    # ccxt y Binance limitan el clientOrderId a 36 caracteres
    coid = client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0)
    assert len(coid) <= 36
    assert len(coid) == 35 and coid.startswith("ct-")
    int(coid[3:], 16)  # 32 caracteres hexadecimales


def test_client_order_id_separator_prevents_field_collisions():
    a = client_order_id("ab", "c", "BTC/USDT", Side.BUY, T0)
    b = client_order_id("a", "bc", "BTC/USDT", Side.BUY, T0)
    assert a != b


def test_client_order_id_same_instant_in_other_timezone_is_equal():
    plus2 = timezone(timedelta(hours=2))
    assert client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0) == \
           client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0.astimezone(plus2))


def test_client_order_id_rejects_naive_datetime():
    with pytest.raises(ValueError):
        client_order_id("s1", "t1", "BTC/USDT", Side.BUY, datetime(2026, 11, 4, 12))


# --- reserva ------------------------------------------------------------

def test_reserve_intent_returns_true_first_time(repo):
    assert repo.reserve_intent("c1", PAYLOAD) is True


def test_reserve_intent_returns_false_on_duplicate(repo):
    # spec §4.7: la misma señal reprocesada no genera una segunda orden
    repo.reserve_intent("c1", PAYLOAD)
    assert repo.reserve_intent("c1", PAYLOAD) is False


def test_reserve_intent_duplicate_does_not_overwrite_existing(repo):
    repo.reserve_intent("c1", PAYLOAD)
    repo.mark_intent_sent("c1")
    repo.reserve_intent("c1", {"otro": "payload"})
    (intent,) = repo.unconfirmed_intents()
    assert intent.status is IntentStatus.SENT
    assert intent.payload == PAYLOAD


def test_reserve_intent_does_not_swallow_other_errors(repo, conn):
    conn.execute("DROP TABLE order_intents")
    with pytest.raises(sqlite3.OperationalError):
        repo.reserve_intent("c1", PAYLOAD)


def test_reserved_intent_roundtrip(repo):
    repo.reserve_intent("c1", PAYLOAD)
    (intent,) = repo.unconfirmed_intents()
    assert isinstance(intent, OrderIntent)
    assert intent.client_order_id == "c1"
    assert intent.status is IntentStatus.RESERVED
    assert intent.payload == PAYLOAD
    assert intent.error is None
    assert intent.created_at.tzinfo is not None
    assert abs(intent.created_at - datetime.now(timezone.utc)) < timedelta(seconds=30)


def test_created_at_uses_canonical_fixed_width_format(repo, conn):
    repo.reserve_intent("c1", PAYLOAD)
    raw = conn.execute("SELECT created_at FROM order_intents").fetchone()[0]
    assert len(raw) == len("2026-11-04T12:00:00.000000+00:00")
    assert raw.endswith("+00:00") and raw[19] == "."


def test_order_intent_is_frozen(repo):
    repo.reserve_intent("c1", PAYLOAD)
    (intent,) = repo.unconfirmed_intents()
    with pytest.raises(Exception):
        intent.status = IntentStatus.SENT


# --- máquina de estados -------------------------------------------------

def test_unconfirmed_intents_lists_reserved_and_sent_only(repo):
    repo.reserve_intent("c1", PAYLOAD)
    repo.reserve_intent("c2", PAYLOAD)
    repo.mark_intent_sent("c2")
    repo.reserve_intent("c3", PAYLOAD)
    repo.mark_intent_confirmed("c3", fill_for("c3"))
    repo.reserve_intent("c4", PAYLOAD)
    repo.mark_intent_failed("c4", "rechazada")
    assert {i.client_order_id for i in repo.unconfirmed_intents()} == {"c1", "c2"}


def test_unconfirmed_intents_empty_when_none(repo):
    assert repo.unconfirmed_intents() == []


def test_failed_intent_keeps_error(repo, conn):
    repo.reserve_intent("c1", PAYLOAD)
    repo.mark_intent_sent("c1")
    repo.mark_intent_failed("c1", "rechazada")
    row = conn.execute("SELECT status, error FROM order_intents").fetchone()
    assert (row["status"], row["error"]) == ("FAILED", "rechazada")


def test_confirmed_after_sent(repo, conn):
    repo.reserve_intent("c1", PAYLOAD)
    repo.mark_intent_sent("c1")
    repo.mark_intent_confirmed("c1", fill_for("c1"))
    assert conn.execute("SELECT status FROM order_intents").fetchone()[0] == "CONFIRMED"


def test_confirmed_directly_from_reserved(repo):
    # El backtest/paper no tiene fase de red: RESERVED -> CONFIRMED es legítimo.
    repo.reserve_intent("c1", PAYLOAD)
    repo.mark_intent_confirmed("c1", fill_for("c1"))
    assert repo.unconfirmed_intents() == []


def test_mark_unknown_intent_raises(repo):
    for call in (
        lambda: repo.mark_intent_sent("nope"),
        lambda: repo.mark_intent_confirmed("nope", fill_for("nope")),
        lambda: repo.mark_intent_failed("nope", "x"),
    ):
        with pytest.raises(ValueError):
            call()


def test_terminal_states_cannot_change(repo):
    repo.reserve_intent("c1", PAYLOAD)
    repo.mark_intent_confirmed("c1", fill_for("c1"))
    with pytest.raises(ValueError):
        repo.mark_intent_failed("c1", "tarde")
    with pytest.raises(ValueError):
        repo.mark_intent_sent("c1")
    repo.reserve_intent("c2", PAYLOAD)
    repo.mark_intent_failed("c2", "x")
    with pytest.raises(ValueError):
        repo.mark_intent_confirmed("c2", fill_for("c2"))


def test_sent_cannot_go_back_to_sent_or_reserved(repo):
    repo.reserve_intent("c1", PAYLOAD)
    repo.mark_intent_sent("c1")
    with pytest.raises(ValueError):
        repo.mark_intent_sent("c1")


def test_confirm_rejects_fill_of_other_order(repo):
    repo.reserve_intent("c1", PAYLOAD)
    with pytest.raises(ValueError):
        repo.mark_intent_confirmed("c1", fill_for("otro"))
    assert len(repo.unconfirmed_intents()) == 1
