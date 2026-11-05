"""Gates de riesgo: la última barrera antes de poner dinero.

``evaluate_risk`` es pura: sin E/S, sin red, sin reloj.

Precedencia fija, gana la primera que se cumpla:

1. Apagado por pérdida diaria -> ``SHUTDOWN``. Va primero, antes que cualquier
   rechazo: un bot que sigue evaluando órdenes tras cruzar su límite de pérdida
   diaria no tiene límite de pérdida diaria. ``SHUTDOWN`` no es un rechazo más
   grande sino otra categoría: quien llama debe terminar el ciclo, no pasar a
   la siguiente señal.
2. Exposición máxima por símbolo.
3. Máximo de posiciones abiertas.
4. Gasto máximo diario.

Todos los límites se cruzan con desigualdad estricta: un importe que deja el
total exactamente en el límite se permite; solo se rechaza lo que lo supera.
(El tope duro por orden lo aplica el dimensionado, ``apply_hard_cap``.)

``build_risk_state`` recibe el día UTC por parámetro y no lee el reloj: quien
llama lo fija una sola vez al inicio del ciclo, para que un cambio de día a
mitad de ciclo no mueva la contabilidad. La exposición y el número de posiciones
abiertas también llegan por parámetro: los agrega ``Portfolio``, y tener dos
agregaciones de lo mismo en dos sitios es garantía de que diverjan.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from core.config import RiskConfig
from core.models import RejectReason, Side
from storage.repository import Repository

_ZERO = Decimal(0)


class RiskAction(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    action: RiskAction
    reason: RejectReason | None = None  # solo con REJECT


@dataclass(frozen=True, slots=True)
class RiskState:
    utc_day: date
    spent_usd_today: Decimal
    realized_pnl_usd_today: Decimal
    open_position_count: int
    exposure_by_symbol: Mapping[str, Decimal]


_ALLOW = RiskVerdict(RiskAction.ALLOW)
_SHUTDOWN = RiskVerdict(RiskAction.SHUTDOWN)


def _reject(reason: RejectReason) -> RiskVerdict:
    return RiskVerdict(RiskAction.REJECT, reason)


def evaluate_risk(
    amount_usd: Decimal, symbol: str, state: RiskState, cfg: RiskConfig
) -> RiskVerdict:
    # 1. Apagado por pérdida diaria: antes que cualquier rechazo.
    if state.realized_pnl_usd_today < -cfg.max_daily_loss_usd:
        return _SHUTDOWN

    # 2. Exposición por símbolo.
    current = state.exposure_by_symbol.get(symbol, _ZERO)
    if current + amount_usd > cfg.max_usd_per_token:
        return _reject(RejectReason.MAX_USD_PER_TOKEN)

    # 3. Posiciones abiertas: con el cupo lleno, una más lo supera.
    if state.open_position_count + 1 > cfg.max_open_positions:
        return _reject(RejectReason.MAX_OPEN_POSITIONS)

    # 4. Gasto diario.
    if state.spent_usd_today + amount_usd > cfg.max_usd_per_day:
        return _reject(RejectReason.MAX_USD_PER_DAY)

    return _ALLOW


def build_risk_state(
    repo: Repository,
    utc_day: date,
    exposure_by_symbol: Mapping[str, Decimal],
    open_position_count: int,
) -> RiskState:
    """Estado de riesgo del día ``utc_day`` (ventana UTC semiabierta).

    Gasto: nocional (precio x cantidad) de las compras del día.
    PnL realizado: por cada venta, (precio - entrada de su posición) x cantidad;
    menos las comisiones de todos los fills del día (conservador: las de entrada
    también son dinero perdido).
    """
    start = datetime.combine(utc_day, time.min, tzinfo=timezone.utc)
    fills = repo.fills_between(start, start + timedelta(days=1))

    spent = _ZERO
    realized = _ZERO
    for f in fills:
        realized -= f.fee_usd
        if f.side is Side.BUY:
            spent += f.price * f.quantity
            continue
        position = repo.get_position(f.position_id) if f.position_id is not None else None
        if position is None:
            # Un PnL calculado a ciegas podría ocultar una pérdida: fallar alto.
            raise ValueError(f"sell fill {f.id!r} references unknown position {f.position_id!r}")
        realized += (f.price - position.entry_price) * f.quantity

    return RiskState(
        utc_day=utc_day,
        spent_usd_today=spent,
        realized_pnl_usd_today=realized,
        open_position_count=open_position_count,
        exposure_by_symbol=MappingProxyType(dict(exposure_by_symbol)),
    )
