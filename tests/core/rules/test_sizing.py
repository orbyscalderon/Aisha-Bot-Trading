from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.config import SizingConfig
from core.models import Market, Side, Signal
from core.rules.sizing import SizingContext, apply_hard_cap, calculate_size


def signal_with(trader_amount_usd):
    return Signal(
        id="s1", trader_id="t1", market=Market.CEX, symbol="BTCUSDT", side=Side.BUY,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        trader_amount_usd=trader_amount_usd,
    )


def flat_cfg(amount):
    return SizingConfig(strategy="flat", flat_amount_usd=amount)


def mirror_cfg(pct, cap, flat=Decimal("50")):
    return SizingConfig(strategy="mirror", mirror_pct=Decimal(pct),
                        mirror_cap_usd=Decimal(cap), flat_amount_usd=flat)


def risk_cfg(pct):
    return SizingConfig(strategy="risk", risk_pct=Decimal(pct))


sig = signal_with(None)


def ctx_with(equity=Decimal("1000"), stop=Decimal("15"), slip=10, fee=10):
    return SizingContext(equity_usd=equity, stop_loss_pct=stop, slippage_bps=slip, fee_bps=fee)


DEFAULT_CTX = ctx_with()


def test_flat_ignores_trader_amount():
    r = calculate_size(signal_with(Decimal("5000")), flat_cfg(Decimal("50")), DEFAULT_CTX)
    assert r.amount_usd == Decimal("50") and r.used_fallback is False


def test_mirror_applies_percentage():
    # 10% de 1000 = 100, por debajo del tope de 200
    assert calculate_size(signal_with(Decimal("1000")), mirror_cfg(10, 200), DEFAULT_CTX).amount_usd == Decimal("100")


def test_mirror_respects_cap():
    # 10% de 9000 = 900, se recorta al tope 200
    assert calculate_size(signal_with(Decimal("9000")), mirror_cfg(10, 200), DEFAULT_CTX).amount_usd == Decimal("200")


def test_mirror_falls_back_to_flat_when_amount_absent():
    # spec §4.3: nunca se adivina un tamaño
    r = calculate_size(signal_with(None), mirror_cfg(10, 200, flat=Decimal("50")), DEFAULT_CTX)
    assert r.amount_usd == Decimal("50") and r.used_fallback is True


def test_mirror_falls_back_to_flat_when_amount_is_zero():
    # Review Focus 4: cero no debe producir tamaño cero
    r = calculate_size(signal_with(Decimal("0")), mirror_cfg(10, 200, flat=Decimal("50")), DEFAULT_CTX)
    assert r.amount_usd == Decimal("50") and r.used_fallback is True


def test_mirror_rounds_down_to_two_decimals():
    # 10% de 33.339 = 3.3339 -> 3.33, nunca 3.34
    r = calculate_size(signal_with(Decimal("33.339")), mirror_cfg(10, 200), DEFAULT_CTX)
    assert r.amount_usd == Decimal("3.33")


def test_mirror_without_flat_fallback_raises_when_amount_absent():
    with pytest.raises(ValueError, match="flat_amount_usd"):
        calculate_size(signal_with(None), mirror_cfg(10, 200, flat=None), DEFAULT_CTX)


def test_hard_cap_cuts_absurd_amount():
    assert apply_hard_cap(Decimal("99999"), Decimal("500")) == Decimal("500")


def test_hard_cap_leaves_amount_below_limit():
    assert apply_hard_cap(Decimal("50"), Decimal("500")) == Decimal("50")


# --- estrategia `risk` (spec §4.3.1) ---

def test_risk_sizes_so_stop_loses_the_declared_fraction():
    # capital 1000, riesgo 1% -> 10 USD en juego
    # fracción estresada = 0.15 + 2*10/10000 + 2*10/10000 = 0.154
    # importe = 10 / 0.154 = 64.93 (ROUND_DOWN a 2 decimales)
    ctx = SizingContext(equity_usd=Decimal("1000"), stop_loss_pct=Decimal("15"),
                        slippage_bps=10, fee_bps=10)
    assert calculate_size(sig, risk_cfg(pct=1), ctx).amount_usd == Decimal("64.93")


def test_risk_scales_with_equity():
    # el doble de capital, el doble de importe
    big = calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("2000"))).amount_usd
    small = calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("1000"))).amount_usd
    # ROUND_DOWN: 129.870... -> 129.87 y 64.935... -> 64.93, así que el doble
    # exacto solo se cumple salvo un céntimo de redondeo.
    assert abs(big - small * 2) <= Decimal("0.01")
    assert big > small * 2 - Decimal("0.01")
    # Con una fracción que divide exacto no hay redondeo y la igualdad es estricta.
    exact = dict(stop=Decimal("20"), slip=0, fee=0)
    assert calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("2000"), **exact)).amount_usd ==         calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("1000"), **exact)).amount_usd * 2


def test_risk_sizes_smaller_when_stop_is_wider():
    # un stop más lejano exige una posición más pequeña para arriesgar lo mismo
    near = calculate_size(sig, risk_cfg(pct=1), ctx_with(stop=Decimal("5"))).amount_usd
    far = calculate_size(sig, risk_cfg(pct=1), ctx_with(stop=Decimal("40"))).amount_usd
    assert far < near


def test_risk_includes_slippage_and_fees_on_both_legs():
    # sin costes la fracción es 0.15 -> 66.66; con ellos sale menos
    clean = calculate_size(sig, risk_cfg(pct=1), ctx_with(slip=0, fee=0)).amount_usd
    assert clean == Decimal("66.66")
    assert calculate_size(sig, risk_cfg(pct=1), ctx_with(slip=10, fee=10)).amount_usd < clean


def test_risk_without_stop_loss_raises():
    # sin stop no hay distancia que medir; la configuración ya lo impide, esto es la segunda red
    with pytest.raises(ValueError, match="stop_loss_pct"):
        calculate_size(sig, risk_cfg(pct=1), ctx_with(stop=None))


def test_risk_with_zero_equity_returns_zero():
    assert calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("0"))).amount_usd == Decimal("0")


def test_risk_ignores_trader_amount_and_never_uses_fallback():
    r = calculate_size(signal_with(Decimal("5000")), risk_cfg(pct=1), DEFAULT_CTX)
    assert r.amount_usd == Decimal("64.93") and r.used_fallback is False
