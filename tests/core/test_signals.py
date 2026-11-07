from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.models import Market, RejectReason, Side, Signal
from core.signals import (
    clamp_cursor,
    detect_new_traders,
    ensure_frozen_allowlist,
    filter_by_allowlist,
)
from storage.db import init_db
from storage.repository import Repository

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def sig(trader_id, id="s1"):
    return Signal(
        id=id, trader_id=trader_id, market=Market.CEX, symbol="BTC",
        side=Side.BUY, timestamp=NOW, trader_amount_usd=Decimal("100"),
    )


@pytest.fixture
def repo(tmp_path):
    c = init_db(tmp_path / "bot.db")
    yield Repository(c)
    c.close()


def test_ensure_frozen_allowlist_persists_first_value(repo):
    assert ensure_frozen_allowlist("s1", {"t1", "t2"}, repo) == frozenset({"t1", "t2"})
    assert repo.get_frozen_allowlist("s1") == frozenset({"t1", "t2"})


def test_second_freeze_in_same_session_returns_original(repo):
    # spec §4.1: un trader añadido a mitad de sesión NO se copia
    ensure_frozen_allowlist("s1", {"t1"}, repo)
    assert ensure_frozen_allowlist("s1", {"t1", "t2"}, repo) == frozenset({"t1"})


def test_new_session_can_freeze_different_allowlist(repo):
    ensure_frozen_allowlist("s1", {"t1"}, repo)
    assert ensure_frozen_allowlist("s2", {"t1", "t2"}, repo) == frozenset({"t1", "t2"})


def test_filter_rejects_signals_from_unknown_trader():
    accepted, rejected = filter_by_allowlist([sig("t1"), sig("t9")], frozenset({"t1"}))
    assert [s.trader_id for s in accepted] == ["t1"]
    assert rejected == [(sig("t9"), RejectReason.NOT_IN_ALLOWLIST)]


def test_detect_new_traders_reports_additions_only():
    assert detect_new_traders({"t1", "t2"}, frozenset({"t1"})) == {"t2"}
    assert detect_new_traders({"t1"}, frozenset({"t1", "t2"})) == set()


def test_clamp_cursor_rejects_future_timestamp():
    # Review Focus 5: reloj de la fuente adelantado no debe saltarse señales reales
    assert clamp_cursor(NOW + timedelta(hours=5), NOW) == NOW


def test_clamp_cursor_keeps_past_timestamp():
    assert clamp_cursor(NOW - timedelta(minutes=5), NOW) == NOW - timedelta(minutes=5)


def test_empty_allowlist_is_frozen_and_not_refrozen(repo):
    # Un frozenset vacío es falso pero no es "ausente": seguir a cero traders
    # es un estado legítimo y no debe reescribirse en la segunda llamada.
    assert ensure_frozen_allowlist("s1", set(), repo) == frozenset()
    assert ensure_frozen_allowlist("s1", {"t1"}, repo) == frozenset()
