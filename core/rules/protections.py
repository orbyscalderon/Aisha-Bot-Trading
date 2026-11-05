"""Protecciones: limitan CON QUÉ FRECUENCIA se insiste, no cuánto se arriesga.

Los gates de ``risk.py`` acotan el tamaño por operación, por token y por día.
Aquí se cubre lo que no ven: insistir en la misma apuesta perdedora y tratar
como diversificación lo que es una sola apuesta correlacionada.

``evaluate_protections`` es pura: sin E/S ni reloj (``now`` llega por parámetro).

Solo bloquean ENTRADAS, nunca salidas. Una posición abierta se sigue vigilando
y cerrando pase lo que pase; bloquear salidas convertiría una protección en una
trampa. Por eso la función ni siquiera recibe una posición que cerrar.

Orden fijo, devuelve en la primera coincidencia (el veredicto más global es el
más informativo):

1. Racha de stop-loss. Solo cuentan los cierres ``STOP_LOSS``.
2. Drawdown máximo.
3. Cooldown del símbolo tras un cierre en pérdida.
4. Exposición del grupo correlacionado.

Todos los umbrales con desigualdad estricta. Un 0 en la configuración
desactiva la protección.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from core.config import ProtectionsConfig
from core.models import ExitReason, Position, RejectReason

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class ProtectionVerdict:
    blocked: bool
    reason: RejectReason | None = None
    until: datetime | None = None  # cuándo deja de aplicar, si es por tiempo


@dataclass(frozen=True, slots=True)
class ProtectionState:
    # Posiciones cerradas recientes (el llamador acota el histórico a la ventana
    # más larga que necesite); las abiertas no cuentan.
    closed_positions: Sequence[Position]
    peak_equity_usd: Decimal
    equity_usd: Decimal
    exposure_by_symbol: Mapping[str, Decimal]
    groups: Mapping[str, Sequence[str]] = field(default_factory=dict)


_ALLOW = ProtectionVerdict(False)


def group_of(symbol: str, groups: Mapping[str, Sequence[str]]) -> str:
    """Grupo que contiene el símbolo, o el propio símbolo si no está en ninguno."""
    for name, symbols in groups.items():
        if symbol in symbols:
            return name
    return symbol


def _is_loss(p: Position) -> bool:
    return p.exit_price is not None and p.exit_price < p.entry_price


def _streak(state: ProtectionState, cfg: ProtectionsConfig, now: datetime) -> ProtectionVerdict:
    if cfg.stop_loss_streak <= 0:
        return _ALLOW
    window_start = now - timedelta(hours=cfg.streak_window_hours)
    stops = [
        p.closed_at
        for p in state.closed_positions
        if p.exit_reason is ExitReason.STOP_LOSS and p.closed_at is not None and p.closed_at > window_start
    ]
    if len(stops) < cfg.stop_loss_streak:
        return _ALLOW
    until = max(stops) + timedelta(hours=cfg.streak_pause_hours)
    if now < until:
        return ProtectionVerdict(True, RejectReason.STOP_LOSS_STREAK, until)
    return _ALLOW


def _drawdown(state: ProtectionState, cfg: ProtectionsConfig) -> ProtectionVerdict:
    if cfg.max_drawdown_pct <= 0 or state.peak_equity_usd <= 0:
        return _ALLOW
    drawdown_pct = (state.peak_equity_usd - state.equity_usd) / state.peak_equity_usd * _HUNDRED
    if drawdown_pct > cfg.max_drawdown_pct:
        return ProtectionVerdict(True, RejectReason.MAX_DRAWDOWN)
    return _ALLOW


def _cooldown(symbol: str, state: ProtectionState, cfg: ProtectionsConfig, now: datetime) -> ProtectionVerdict:
    if cfg.cooldown_minutes <= 0:
        return _ALLOW
    losses = [
        p.closed_at
        for p in state.closed_positions
        if p.symbol == symbol and p.closed_at is not None and _is_loss(p)
    ]
    if not losses:
        return _ALLOW
    until = max(losses) + timedelta(minutes=cfg.cooldown_minutes)
    if now < until:
        return ProtectionVerdict(True, RejectReason.COOLDOWN, until)
    return _ALLOW


def _group_exposure(
    symbol: str, amount_usd: Decimal, state: ProtectionState, cfg: ProtectionsConfig
) -> ProtectionVerdict:
    if cfg.max_usd_per_group <= 0:
        return _ALLOW
    group = group_of(symbol, state.groups)
    total = amount_usd + sum(
        (usd for sym, usd in state.exposure_by_symbol.items() if group_of(sym, state.groups) == group),
        _ZERO,
    )
    if total > cfg.max_usd_per_group:
        return ProtectionVerdict(True, RejectReason.MAX_USD_PER_GROUP)
    return _ALLOW


def evaluate_protections(
    symbol: str,
    amount_usd: Decimal,
    state: ProtectionState,
    cfg: ProtectionsConfig,
    now: datetime,
) -> ProtectionVerdict:
    for verdict in (
        _streak(state, cfg, now),
        _drawdown(state, cfg),
        _cooldown(symbol, state, cfg, now),
        _group_exposure(symbol, amount_usd, state, cfg),
    ):
        if verdict.blocked:
            return verdict
    return _ALLOW
