"""Candados de seguridad: prefijo de modo, kill switch y circuit breaker."""

from __future__ import annotations

from pathlib import Path

from core.models import Mode

KILL_SWITCH_FILENAME = "STOP"


def mode_prefix(mode: Mode) -> str:
    """Prefijo con el que empieza toda salida del bot."""
    return f"mode: {mode.value}"


def is_kill_switch_active(root: Path) -> bool:
    """True si existe un fichero STOP en `root` (la raíz se recibe, no se deduce)."""
    return (root / KILL_SWITCH_FILENAME).exists()


class CircuitBreaker:
    """Se abre tras `threshold` fallos CONSECUTIVOS; un éxito reinicia la cuenta."""

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def record_success(self) -> None:
        self._consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        return self._consecutive_failures >= self._threshold
