"""Regla de tamaño de posición: cuánto dinero se pone en cada operación.

Función pura: sin E/S, sin red, sin reloj. El redondeo de ``mirror`` y de
``risk`` es a 2 decimales hacia abajo: nunca se gasta más de lo que la regla
permite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from core.config import SizingConfig
from core.models import Signal

_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_BPS = Decimal(10000)


@dataclass(frozen=True, slots=True)
class SizingContext:
    """Datos del entorno que la regla necesita y que no están en la señal."""

    equity_usd: Decimal
    stop_loss_pct: Decimal | None
    slippage_bps: int
    fee_bps: int


@dataclass(frozen=True, slots=True)
class SizeResult:
    amount_usd: Decimal
    used_fallback: bool


def _round_down(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_DOWN)


def _flat_fallback(cfg: SizingConfig) -> Decimal:
    if cfg.flat_amount_usd is None:
        raise ValueError("sizing.flat_amount_usd es necesario como importe de reserva con strategy=mirror")
    return cfg.flat_amount_usd


def _risk_amount(cfg: SizingConfig, ctx: SizingContext) -> Decimal:
    if ctx.stop_loss_pct is None:
        raise ValueError("strategy=risk exige stop_loss_pct en el contexto: sin stop no hay distancia que medir")
    if ctx.stop_loss_pct <= 0:
        raise ValueError("stop_loss_pct debe ser positivo")
    if cfg.risk_pct is None:
        raise ValueError("sizing.risk_pct es obligatorio con strategy=risk")
    if ctx.equity_usd < 0:
        raise ValueError("equity_usd no puede ser negativo")
    if ctx.equity_usd == 0:
        # Capital cero es un estado legítimo: importe cero, no error.
        return Decimal(0)
    # El factor 2: comisión y deslizamiento en la pata de entrada y en la de
    # salida; el stop no se ejecuta en su precio exacto sino algo peor.
    stressed_fraction = (
        ctx.stop_loss_pct / _HUNDRED
        + 2 * Decimal(ctx.slippage_bps) / _BPS
        + 2 * Decimal(ctx.fee_bps) / _BPS
    )
    return _round_down((ctx.equity_usd * cfg.risk_pct / _HUNDRED) / stressed_fraction)


def calculate_size(signal: Signal, cfg: SizingConfig, ctx: SizingContext) -> SizeResult:
    if cfg.strategy == "flat":
        assert cfg.flat_amount_usd is not None  # garantizado por SizingConfig
        return SizeResult(cfg.flat_amount_usd, used_fallback=False)

    if cfg.strategy == "mirror":
        if not signal.has_trader_amount():
            # Spec §4.3: nunca se adivina un tamaño.
            return SizeResult(_flat_fallback(cfg), used_fallback=True)
        assert signal.trader_amount_usd is not None
        assert cfg.mirror_pct is not None and cfg.mirror_cap_usd is not None
        raw = signal.trader_amount_usd * cfg.mirror_pct / _HUNDRED
        return SizeResult(_round_down(min(raw, cfg.mirror_cap_usd)), used_fallback=False)

    if cfg.strategy == "risk":
        return SizeResult(_risk_amount(cfg, ctx), used_fallback=False)

    raise ValueError(f"strategy desconocida: {cfg.strategy}")


def apply_hard_cap(amount_usd: Decimal, hard_max_usd: Decimal) -> Decimal:
    return min(amount_usd, hard_max_usd)
