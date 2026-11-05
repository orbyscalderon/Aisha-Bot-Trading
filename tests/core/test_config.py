import copy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from core.config import ConfigError, load_config
from core.models import Market, Mode

BASE = {
    "mode": "paper",
    "sizing": {"strategy": "flat", "flat_amount_usd": 25},
    "exits": {"stop_loss_pct": 15, "primary": "copy_sell", "time_stop_hours": 72, "trailing": False},
    "risk": {
        "max_usd_per_token": 50,
        "max_open_positions": 5,
        "max_usd_per_day": 150,
        "max_daily_loss_usd": 50,
        "hard_max_order_usd": 50,
    },
    "cadence": {"entry_cron": "0 * * * *", "exit_interval_seconds": 60},
}


@pytest.fixture
def yaml_with(tmp_path):
    counter = {"n": 0}

    def _make(mode=None, stop_loss_pct="keep", strategy=None, mirror_pct=None,
              mirror_cap_usd=None, primary="keep", hard_max_order_usd=None,
              cex_slippage=None, raw=None):
        data = copy.deepcopy(BASE) if raw is None else raw
        if raw is None:
            if mode is not None:
                data["mode"] = mode
            if stop_loss_pct != "keep":
                data["exits"]["stop_loss_pct"] = stop_loss_pct
            if strategy is not None:
                data["sizing"]["strategy"] = strategy
                data["sizing"]["mirror_pct"] = mirror_pct
                data["sizing"]["mirror_cap_usd"] = mirror_cap_usd
            if primary != "keep":
                data["exits"]["primary"] = primary
            if hard_max_order_usd is not None:
                data["risk"]["hard_max_order_usd"] = hard_max_order_usd
            if cex_slippage is not None:
                data["execution"] = {"cex": {"slippage_bps": cex_slippage}}
        counter["n"] += 1
        path = tmp_path / f"cfg{counter['n']}.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _make


def test_live_mode_requires_stop_loss(yaml_with):
    # spec §4.4: en modo real el stop-loss es obligatorio
    with pytest.raises(ConfigError, match="stop_loss_pct"):
        load_config(yaml_with(mode="live", stop_loss_pct=None))


def test_paper_mode_allows_absent_stop_loss(yaml_with):
    assert load_config(yaml_with(mode="paper", stop_loss_pct=None)).exits.stop_loss_pct is None


def test_mirror_strategy_requires_pct_and_cap(yaml_with):
    with pytest.raises(ConfigError, match="mirror_cap_usd"):
        load_config(yaml_with(strategy="mirror", mirror_pct=10, mirror_cap_usd=None))


def test_exactly_one_primary_exit(yaml_with):
    with pytest.raises(ConfigError, match="primary"):
        load_config(yaml_with(primary=None))


def test_take_profit_primary_requires_take_profit_pct(yaml_with):
    with pytest.raises(ConfigError, match="take_profit_pct"):
        load_config(yaml_with(primary="take_profit"))


def test_time_stop_primary_requires_hours(yaml_with):
    data = copy.deepcopy(BASE)
    data["exits"]["primary"] = "time_stop"
    del data["exits"]["time_stop_hours"]
    with pytest.raises(ConfigError, match="time_stop_hours"):
        load_config(yaml_with(raw=data))


@pytest.mark.parametrize("field", [
    "max_usd_per_token", "max_open_positions", "max_usd_per_day",
    "max_daily_loss_usd", "hard_max_order_usd",
])
def test_all_risk_limits_must_be_positive(yaml_with, field):
    data = copy.deepcopy(BASE)
    data["risk"][field] = 0
    with pytest.raises(ConfigError, match=field):
        load_config(yaml_with(raw=data))


def test_hard_max_order_usd_must_be_positive(yaml_with):
    with pytest.raises(ConfigError):
        load_config(yaml_with(hard_max_order_usd=0))


def test_validation_error_message_is_preserved_as_text(yaml_with):
    with pytest.raises(ConfigError) as exc:
        load_config(yaml_with(hard_max_order_usd=-5))
    assert "hard_max_order_usd" in str(exc.value)
    assert exc.value.__cause__ is not None


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_money_amounts_are_decimal(yaml_with):
    c = load_config(yaml_with())
    assert isinstance(c.sizing.flat_amount_usd, Decimal)
    assert isinstance(c.risk.hard_max_order_usd, Decimal)
    assert isinstance(c.exits.stop_loss_pct, Decimal)


def test_fractional_amount_keeps_decimal_precision(tmp_path):
    p = tmp_path / "c.yaml"
    data = copy.deepcopy(BASE)
    data["sizing"]["flat_amount_usd"] = 0.1
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_config(p).sizing.flat_amount_usd == Decimal("0.1")


def test_fingerprint_stable_across_key_order(yaml_with):
    a = copy.deepcopy(BASE)
    b = {k: a[k] for k in reversed(list(a))}
    b["risk"] = {k: a["risk"][k] for k in reversed(list(a["risk"]))}
    assert load_config(yaml_with(raw=a)).fingerprint() == load_config(yaml_with(raw=b)).fingerprint()


def test_fingerprint_changes_with_threshold(yaml_with):
    a = copy.deepcopy(BASE)
    b = copy.deepcopy(BASE)
    b["exits"]["stop_loss_pct"] = 10
    assert load_config(yaml_with(raw=a)).fingerprint() != load_config(yaml_with(raw=b)).fingerprint()


def test_fingerprint_changes_with_execution_threshold(yaml_with):
    assert load_config(yaml_with()).fingerprint() != load_config(yaml_with(cex_slippage=42)).fingerprint()


def test_execution_defaults_per_market(yaml_with):
    # spec §6.2: el deslizamiento por defecto es mayor en Solana que en CEX
    ex = load_config(yaml_with()).execution
    assert set(ex) == set(Market)
    assert ex[Market.CEX].slippage_bps == 10 and ex[Market.CEX].fee_bps == 10
    assert ex[Market.SOLANA].slippage_bps == 100 and ex[Market.SOLANA].fee_bps == 25
    assert ex[Market.STOCKS].slippage_bps == 5 and ex[Market.STOCKS].fee_bps == 0
    assert ex[Market.FOMO].slippage_bps == 100 and ex[Market.FOMO].fee_bps == 25


def test_quantity_precision_defaults_per_market(yaml_with):
    ex = load_config(yaml_with()).execution
    assert (ex[Market.CEX].quantity_precision, ex[Market.SOLANA].quantity_precision) == (8, 6)
    assert (ex[Market.STOCKS].quantity_precision, ex[Market.FOMO].quantity_precision) == (4, 6)


def test_execution_values_overridable_from_yaml(yaml_with):
    ex = load_config(yaml_with(cex_slippage=42)).execution
    assert ex[Market.CEX].slippage_bps == 42
    # lo no mencionado conserva su valor por defecto, y los demás mercados siguen presentes
    assert ex[Market.CEX].fee_bps == 10
    assert set(ex) == set(Market)


def test_example_config_loads_with_conservative_defaults():
    c = load_config(Path("config/config.example.yaml"))
    assert c.mode is Mode.PAPER
    assert c.sizing.flat_amount_usd == Decimal("25")
    assert c.exits.stop_loss_pct == Decimal("15") and c.exits.trailing is False
    assert (c.risk.max_open_positions, c.risk.max_usd_per_token) == (5, Decimal("50"))
    assert c.risk.max_daily_loss_usd == Decimal("50")
    assert c.risk.hard_max_order_usd == Decimal("50")
    assert c.risk.max_usd_per_day == Decimal("150")
    assert c.exits.primary == "copy_sell" and c.exits.time_stop_hours == 72
    assert c.cadence.entry_cron == "0 * * * *" and c.cadence.exit_interval_seconds == 60


def test_env_example_has_no_secret_values():
    text = Path("config/.env.example").read_text(encoding="utf-8")
    assert "retirada" in text.lower()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            assert key and value == "", f"{key} no debe llevar valor en la plantilla"
