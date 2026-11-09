"""Candados de seguridad: prefijo de modo, kill switch, pausa y circuit breaker.

Kill switch y pausa son dos cosas distintas a propósito:

- **Kill switch** (fichero ``STOP``): para las APERTURAS y es de ida. Este módulo no ofrece
  ninguna forma de desactivarlo: hay que ir a la máquina y borrar el fichero. La asimetría es
  deliberada (diseño §10.3): parar tiene que ser siempre más fácil que arrancar, porque un
  mensaje de chat lo puede mandar un móvil desbloqueado, alguien que lea tu pantalla o un LLM
  que malinterprete una frase.
- **Pausa** (fichero ``PAUSE``): también para las aperturas, pero se deshace desde el chat. Es
  el mando del día a día.

Ninguno de los dos toca las SALIDAS. Un bot que al pausarse dejara de vigilar los stop-loss de
lo que ya tiene abierto sería peor que uno sin pausa: te deja posiciones y nadie que las cierre.

Son ficheros y no filas en la base de datos para que se puedan accionar y ver sin el bot
delante: ``type STOP`` en una terminal, o crearlo a mano si todo lo demás falla.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.models import Mode

KILL_SWITCH_FILENAME = "STOP"
PAUSE_FILENAME = "PAUSE"


def mode_prefix(mode: Mode) -> str:
    """Prefijo con el que empieza toda salida del bot."""
    return f"mode: {mode.value}"


def is_kill_switch_active(root: Path) -> bool:
    """True si existe un fichero STOP en `root` (la raíz se recibe, no se deduce)."""
    return (root / KILL_SWITCH_FILENAME).exists()


def is_paused(root: Path) -> bool:
    """True si existe un fichero PAUSE en `root`: las aperturas están detenidas."""
    return (Path(root) / PAUSE_FILENAME).exists()


def _write_flag(root: Path, filename: str, reason: str) -> None:
    """Escribe el fichero de bandera con quién y cuándo, para que se explique solo.

    Sobrescribe si ya existe: accionarlo dos veces no es un error, y el motivo más reciente
    es el que importa.
    """
    path = Path(root) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    path.write_text(f"{stamp}\n{reason}\n", encoding="utf-8")


def activate_kill_switch(root: Path, reason: str) -> None:
    """Activa el kill switch. NO existe la operación inversa: ver la nota del módulo."""
    _write_flag(root, KILL_SWITCH_FILENAME, reason)


def pause(root: Path, reason: str) -> None:
    """Detiene las aperturas. Las salidas siguen funcionando."""
    _write_flag(root, PAUSE_FILENAME, reason)


def resume(root: Path) -> bool:
    """Reanuda las aperturas. Devuelve si de verdad había una pausa que deshacer."""
    path = Path(root) / PAUSE_FILENAME
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


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
