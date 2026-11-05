"""Qué señales se copian y cuáles no se copian dos veces.

Dos reglas:

1. Allowlist congelada. Al arrancar una sesión se fija la lista de traders
   seguidos y no cambia en toda la sesión. Un trader añadido a mitad de sesión
   no se copia: evita que un cambio accidental en la plataforma, o un alta por
   curiosidad, genere operaciones que nadie decidió. ``detect_new_traders``
   sirve para avisar de esas altas, no para incorporarlas.
2. Recorte de cursor. ``clamp_cursor`` nunca deja avanzar el cursor más allá
   del presente: si la fuente tiene el reloj adelantado y reporta una operación
   con fecha futura, avanzar hasta ahí se saltaría señales reales que lleguen
   después con fecha intermedia.

``filter_by_allowlist``, ``detect_new_traders`` y ``clamp_cursor`` son puras.
``ensure_frozen_allowlist`` es la única con E/S, y el ``Repository`` entra por
parámetro. No se llama igual que ``Repository.freeze_allowlist`` a propósito:
aquélla escribe sin preguntar y lanza si ya existe; ésta consulta primero. El
motor llama siempre a ésta.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime

from core.models import RejectReason, Signal
from storage.repository import Repository


def ensure_frozen_allowlist(
    session_id: str, following: Iterable[str], repo: Repository
) -> frozenset[str]:
    """Devuelve la allowlist de la sesión; la congela con ``following`` solo si no existe.

    Si ya había una congelada, ``following`` se ignora por completo.
    """
    frozen = repo.get_frozen_allowlist(session_id)
    if frozen is not None:
        return frozen
    traders = frozenset(following)  # materializar: ``following`` puede ser un iterador
    repo.freeze_allowlist(session_id, traders)
    return traders


def filter_by_allowlist(
    signals: Iterable[Signal], allowlist: Collection[str]
) -> tuple[list[Signal], list[tuple[Signal, RejectReason]]]:
    """Separa las señales de traders permitidos de las rechazadas, con su motivo."""
    accepted: list[Signal] = []
    rejected: list[tuple[Signal, RejectReason]] = []
    for signal in signals:
        if signal.trader_id in allowlist:
            accepted.append(signal)
        else:
            rejected.append((signal, RejectReason.NOT_IN_ALLOWLIST))
    return accepted, rejected


def detect_new_traders(following: Iterable[str], frozen: Collection[str]) -> set[str]:
    """Traders seguidos ahora que no están en la allowlist congelada (solo altas)."""
    return set(following) - set(frozen)


def clamp_cursor(candidate: datetime, now: datetime) -> datetime:
    """``min(candidate, now)``: el cursor nunca avanza más allá del presente."""
    return min(candidate, now)
