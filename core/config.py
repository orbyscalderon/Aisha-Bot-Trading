"""Configuración validada del bot.

Impide arrancar con parámetros peligrosos o incoherentes: las reglas cruzadas
se comprueban al cargar y cualquier fallo se eleva como ``ConfigError`` con el
mensaje original de pydantic, para que la CLI lo muestre como texto.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.models import Market, Mode


class ConfigError(Exception):
    """Configuración inválida o ilegible."""


class _Strict(BaseModel):
    # extra="forbid": una errata en una clave de riesgo no debe ignorarse en silencio.
    model_config = ConfigDict(extra="forbid", frozen=True)


class SizingConfig(_Strict):
    strategy: Literal["flat", "mirror"]
    flat_amount_usd: Decimal | None = Field(default=None, gt=0)
    mirror_pct: Decimal | None = Field(default=None, gt=0, le=100)
    mirror_cap_usd: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_strategy(self) -> SizingConfig:
        if self.strategy == "flat" and self.flat_amount_usd is None:
            raise ValueError("sizing.flat_amount_usd es obligatorio con strategy=flat")
        if self.strategy == "mirror":
            if self.mirror_pct is None:
                raise ValueError("sizing.mirror_pct es obligatorio con strategy=mirror")
            if self.mirror_cap_usd is None:
                raise ValueError("sizing.mirror_cap_usd es obligatorio con strategy=mirror")
        return self


class ExitsConfig(_Strict):
    stop_loss_pct: Decimal | None = Field(default=None, gt=0, le=100)
    take_profit_pct: Decimal | None = Field(default=None, gt=0)
    time_stop_hours: int | None = Field(default=None, gt=0)
    # Obligatoria y de un único valor: exactamente una salida primaria.
    primary: Literal["copy_sell", "take_profit", "time_stop"]
    trailing: bool = False

    @model_validator(mode="after")
    def _check_primary(self) -> ExitsConfig:
        if self.primary == "take_profit" and self.take_profit_pct is None:
            raise ValueError("exits.take_profit_pct es obligatorio con primary=take_profit")
        if self.primary == "time_stop" and self.time_stop_hours is None:
            raise ValueError("exits.time_stop_hours es obligatorio con primary=time_stop")
        return self


class RiskConfig(_Strict):
    max_usd_per_token: Decimal = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    max_usd_per_day: Decimal = Field(gt=0)
    max_daily_loss_usd: Decimal = Field(gt=0)
    hard_max_order_usd: Decimal = Field(gt=0)


class CadenceConfig(_Strict):
    entry_cron: str = Field(min_length=1)
    exit_interval_seconds: int = Field(gt=0)


class ExecutionConfig(_Strict):
    slippage_bps: int = Field(ge=0)
    fee_bps: int = Field(ge=0)
    quantity_precision: int = Field(ge=0)


# Spec §6.2: el deslizamiento es ajustable y mayor donde hay menos liquidez.
_EXECUTION_DEFAULTS: dict[Market, dict[str, int]] = {
    Market.CEX: {"slippage_bps": 10, "fee_bps": 10, "quantity_precision": 8},
    Market.SOLANA: {"slippage_bps": 100, "fee_bps": 25, "quantity_precision": 6},
    Market.STOCKS: {"slippage_bps": 5, "fee_bps": 0, "quantity_precision": 4},
    Market.FOMO: {"slippage_bps": 100, "fee_bps": 25, "quantity_precision": 6},
}


class BotConfig(_Strict):
    mode: Mode
    sizing: SizingConfig
    exits: ExitsConfig
    risk: RiskConfig
    cadence: CadenceConfig
    execution: dict[Market, ExecutionConfig] = Field(default_factory=dict, validate_default=True)
    allowlist: list[str] = Field(default_factory=list)

    @field_validator("execution", mode="before")
    @classmethod
    def _fill_execution_defaults(cls, value: Any) -> dict[Market, Any]:
        overrides = value or {}
        if not isinstance(overrides, dict):
            raise ValueError("execution debe ser un mapa mercado -> parámetros")
        try:
            by_market = {Market(k): v for k, v in overrides.items()}
        except ValueError as exc:
            raise ValueError(f"mercado desconocido en execution: {exc}") from exc
        merged: dict[Market, Any] = {}
        for market, defaults in _EXECUTION_DEFAULTS.items():
            override = by_market.get(market) or {}
            if not isinstance(override, dict):
                raise ValueError(f"execution.{market.value} debe ser un mapa")
            merged[market] = {**defaults, **override}
        return merged

    @model_validator(mode="after")
    def _check_cross_rules(self) -> BotConfig:
        if self.mode is Mode.LIVE and self.exits.stop_loss_pct is None:
            raise ValueError("exits.stop_loss_pct es obligatorio en modo live")
        return self

    def fingerprint(self) -> str:
        """sha256 del contenido normalizado: cambia con cualquier umbral, no con el orden de claves."""

        def _default(obj: Any) -> str:
            if isinstance(obj, Decimal):
                # 25 y 25.0 son el mismo umbral.
                return format(obj.normalize(), "f")
            return str(obj)

        payload = json.dumps(self.model_dump(), sort_keys=True, default=_default)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_config(yaml_path: Path) -> BotConfig:
    try:
        raw = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"No se pudo leer la configuración {yaml_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"La configuración {yaml_path} debe ser un mapa YAML")
    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
