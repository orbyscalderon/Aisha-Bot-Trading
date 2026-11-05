"""Reglas de salida: cuándo se cierra una posición y por qué motivo.

Función pura: sin E/S, sin red, sin reloj (``now`` llega por parámetro).

Precedencia fija (spec §4.4), gana la primera que se cumpla:

1. stop-loss
2. copy-sell
3. take-profit (con trailing desde el pico si está activo)
4. time-stop

El stop-loss va primero a propósito: ante condiciones simultáneas, proteger
capital precede a perseguir ganancia. Y el motivo registrado es el de la regla
que ganó, porque el análisis posterior depende de él.

Todos los umbrales se cruzan con desigualdad estricta: tocarlos no dispara.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core.config import ExitsConfig
from core.models import ExitReason, Position, Quote

_ONE = Decimal(1)
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: ExitReason
    fraction: Decimal  # 1 = cierre total


def next_peak_price(position: Position, quote: Quote) -> Decimal:
    """Pico actualizado: solo sube, nunca baja."""
    return max(position.peak_price, quote.price)


def _stop_loss_hit(position: Position, quote: Quote, cfg: ExitsConfig) -> bool:
    if cfg.stop_loss_pct is None:
        return False
    return position.pct_change(quote.price) < -cfg.stop_loss_pct


def _take_profit_hit(position: Position, quote: Quote, cfg: ExitsConfig) -> bool:
    if cfg.take_profit_pct is None:
        return False
    if not cfg.trailing:
        return position.pct_change(quote.price) > cfg.take_profit_pct
    # Trailing: se arma cuando el pico supera entrada + take_profit_pct y cierra
    # cuando el precio cae más de take_profit_pct desde ese pico (no desde la
    # entrada). La distancia reutiliza take_profit_pct: la config no tiene un
    # parámetro de trailing propio.
    peak = next_peak_price(position, quote)
    if position.pct_change(peak) <= cfg.take_profit_pct:
        return False
    drop_from_peak = (peak - quote.price) / peak * _HUNDRED
    return drop_from_peak > cfg.take_profit_pct


def _time_stop_hit(position: Position, now: datetime, cfg: ExitsConfig) -> bool:
    if cfg.time_stop_hours is None:
        return False
    return now - position.opened_at > timedelta(hours=cfg.time_stop_hours)


def evaluate_exit(
    position: Position,
    quote: Quote,
    now: datetime,
    cfg: ExitsConfig,
    trader_sold_fraction: Decimal | None,
) -> ExitDecision | None:
    # 1. Stop-loss: siempre que esté configurado, sea cual sea la primaria.
    if _stop_loss_hit(position, quote, cfg):
        return ExitDecision(ExitReason.STOP_LOSS, _ONE)

    # 2. Copy-sell: solo si es la primaria. Espeja la fracción vendida.
    if cfg.primary == "copy_sell" and trader_sold_fraction is not None and trader_sold_fraction > 0:
        return ExitDecision(ExitReason.COPY_SELL, min(trader_sold_fraction, _ONE))

    # 3. Take-profit: solo si es la primaria.
    if cfg.primary == "take_profit" and _take_profit_hit(position, quote, cfg):
        return ExitDecision(ExitReason.TAKE_PROFIT, _ONE)

    # 4. Time-stop: primaria o red de seguridad si hay time_stop_hours.
    if _time_stop_hit(position, now, cfg):
        return ExitDecision(ExitReason.TIME_STOP, _ONE)

    return None
