from pathlib import Path

from core.models import Mode
from core.safety import CircuitBreaker, is_kill_switch_active, mode_prefix


def test_mode_prefix_declares_mode():
    assert mode_prefix(Mode.PAPER) == "mode: paper"
    assert mode_prefix(Mode.LIVE) == "mode: live"


def test_kill_switch_detects_stop_file(tmp_path: Path):
    assert is_kill_switch_active(tmp_path) is False
    (tmp_path / "STOP").touch()
    assert is_kill_switch_active(tmp_path) is True


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3)
    for _ in range(2):
        cb.record_failure()
    assert cb.is_open is False
    cb.record_failure()
    assert cb.is_open is True


def test_circuit_breaker_resets_on_success():
    # spec §4.8: el contador es de fallos CONSECUTIVOS
    cb = CircuitBreaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.is_open is False
