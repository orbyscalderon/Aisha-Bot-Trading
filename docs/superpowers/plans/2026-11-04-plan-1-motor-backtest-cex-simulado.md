# Plan 1 — Motor, Backtesting y Primer Mercado en Simulado

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir el motor de decisión completo del bot de copy-trading, con backtesting y el primer mercado (CEX) leyendo precios reales, ejecutando siempre en simulado.

**Architecture:** Puertos y adaptadores. `core/` contiene reglas puras sin red ni E/S y no importa nada de `adapters/`. Los adaptadores implementan interfaces abstractas de `ports/`. El mismo `core/engine.py` sirve al backtest, al modo simulado y (en el plan 3) al real.

**Tech Stack:** Python 3.11+, `pydantic` v2 (validación de configuración), `PyYAML`, `ccxt` (precios de exchange), `typer` (CLI), `httpx` (Telegram), `pytest`. SQLite vía `sqlite3` de la stdlib. Importes siempre en `Decimal`.

**Spec:** `docs/superpowers/specs/2026-11-04-bot-copy-trading-multimercado-design.md`

**Alcance de este plan:** fases 1–5 del spec (§11). Quedan fuera, para los planes 2–4: la capa conversacional (§10), la ejecución real (§7) y los adaptadores de FOMO, Solana y acciones (§5.1, §5.3, §5.4).

## Global Constraints

- Python 3.11+.
- Todo importe monetario es `Decimal`, nunca `float` (spec §2).
- `core/` no importa nada de `adapters/` ni de `interfaces/` (spec §3.2). Un test lo verifica.
- Todo mensaje, log y notificación empieza con `mode: paper` o `mode: live` (spec §7.1).
- `get_quote` devuelve precios reales en todos los modos (spec §3.3).
- En este plan, el único `OrderExecutor` que existe es `adapters/paper/executor.py`.
- La configuración no valida en modo `live` sin `stop_loss_pct` (spec §4.4).
- El cursor de una fuente avanza solo si el ciclo termina sin excepción (spec §4.1).
- `.env` y `*.db` ya están en `.gitignore`.

## Review Focus

Clases de entrada que el spec implica y que ninguna tarea exercita de forma natural. Cada línea tiene su test asignado a la tarea que posee el código.

1. **Precio cero, negativo o `None` devuelto por el mercado** — dividir el importe entre el precio para obtener cantidad produce `DivisionByZero` o una cantidad negativa. Debe rechazarse la operación y notificarse, no abrir posición. → Tarea 12, Tarea 15.
2. **Cambio de día UTC a mitad de ciclo** — los límites `max_usd_per_day` y `max_daily_loss_usd` se calculan contra una ventana diaria; si el día cambia entre la evaluación del gate y el registro del fill, el gasto se contabiliza en el día equivocado. El día debe fijarse una vez al inicio del ciclo. → Tarea 6.
3. **Cantidad que redondea a cero** — un importe de 50 USD en un activo que cotiza a 120 000 USD con precisión de 4 decimales da cantidad 0. Debe rechazarse con motivo explícito, no abrir una posición vacía. → Tarea 12.
4. **`trader_amount_usd` ausente o cero en estrategia `mirror`** — el spec (§4.3) exige caer a `flat` y notificar. Cero es distinto de ausente y ambos deben caer a `flat`, nunca producir tamaño cero. → Tarea 4.
5. **Señal con `timestamp` anterior al cursor o en el futuro** — una anterior al cursor indica reproceso y debe ser idempotente; una en el futuro (reloj desincronizado de la fuente) no debe avanzar el cursor más allá de `now`, o se saltarían señales reales. → Tarea 7.

---

### Task 1: Modelos del dominio

**Files:**
- Create: `core/models.py`
- Test: `tests/core/test_models.py`

**Interfaces:**
- Consumes: nada.
- Produces: enums `Side` (`BUY`, `SELL`), `Mode` (`PAPER`, `LIVE`), `Market` (`FOMO`, `CEX`, `SOLANA`, `STOCKS`), `ExitReason` (`STOP_LOSS`, `COPY_SELL`, `TAKE_PROFIT`, `TIME_STOP`, `MANUAL`), `RejectReason` (`MAX_USD_PER_TOKEN`, `MAX_OPEN_POSITIONS`, `MAX_USD_PER_DAY`, `HARD_CAP`, `INVALID_QUOTE`, `ZERO_QUANTITY`, `NOT_IN_ALLOWLIST`, `DUPLICATE`). Dataclasses congeladas `Signal`, `Quote`, `Order`, `Fill`, `Position`.

- [ ] **Step 1: Write the failing test**

```python
def test_position_unrealized_pnl_usd():
    pos = Position(id="p1", signal_id="s1", trader_id="t1", market=Market.CEX,
                   symbol="BTC/USDT", entry_price=Decimal("100"), quantity=Decimal("2"),
                   amount_usd=Decimal("200"), opened_at=T0, peak_price=Decimal("100"))
    assert pos.unrealized_pnl_usd(Decimal("110")) == Decimal("20")

def test_position_pct_change_from_entry():
    # entrada 100, precio 80 -> -20%
    assert pos_at(Decimal("100")).pct_change(Decimal("80")) == Decimal("-20")

def test_models_are_frozen():
    with pytest.raises(FrozenInstanceError):
        pos_at(Decimal("100")).entry_price = Decimal("1")

def test_signal_amount_absent_and_zero_are_distinguishable():
    assert Signal(..., trader_amount_usd=None).has_trader_amount() is False
    assert Signal(..., trader_amount_usd=Decimal("0")).has_trader_amount() is False
    assert Signal(..., trader_amount_usd=Decimal("10")).has_trader_amount() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_models.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'core.models'`

- [ ] **Step 3: Implement los enums y dataclasses en `core/models.py`**

Todas las dataclasses con `@dataclass(frozen=True, slots=True)`. Campos monetarios y de precio en `Decimal`. `Position` lleva `peak_price` (para el trailing), `closed_at`, `exit_reason`, `exit_price` opcionales. Métodos: `Position.unrealized_pnl_usd(price)`, `Position.pct_change(price)` (porcentaje con signo desde `entry_price`), `Signal.has_trader_amount()` (`False` si es `None` o `<= 0`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/models.py tests/core/test_models.py
git commit -m "feat(core): añadir modelos del dominio"
```

---

### Task 2: Configuración validada

**Files:**
- Create: `core/config.py`, `config/config.example.yaml`, `config/.env.example`
- Test: `tests/core/test_config.py`

**Interfaces:**
- Consumes: enums de Tarea 1.
- Produces: `load_config(yaml_path: Path) -> BotConfig`. `BotConfig` con atributos `mode`, `sizing: SizingConfig`, `exits: ExitsConfig`, `risk: RiskConfig`, `cadence: CadenceConfig`, `execution: dict[Market, ExecutionConfig]`, `allowlist: list[str]`. `ExecutionConfig` con `slippage_bps: int`, `fee_bps: int`, `quantity_precision: int`. `BotConfig.fingerprint() -> str` (sha256 del contenido normalizado, para el periodo de sombra del plan 3). Excepción `ConfigError`.

- [ ] **Step 1: Write the failing test**

```python
def test_live_mode_requires_stop_loss():
    # spec §4.4: en modo real el stop-loss es obligatorio
    with pytest.raises(ConfigError, match="stop_loss_pct"):
        load_config(yaml_with(mode="live", stop_loss_pct=None))

def test_paper_mode_allows_absent_stop_loss():
    assert load_config(yaml_with(mode="paper", stop_loss_pct=None)).exits.stop_loss_pct is None

def test_mirror_strategy_requires_pct_and_cap():
    with pytest.raises(ConfigError, match="mirror_cap_usd"):
        load_config(yaml_with(strategy="mirror", mirror_pct=10, mirror_cap_usd=None))

def test_exactly_one_primary_exit():
    with pytest.raises(ConfigError, match="primary"):
        load_config(yaml_with(primary=None))

def test_hard_max_order_usd_must_be_positive():
    with pytest.raises(ConfigError):
        load_config(yaml_with(hard_max_order_usd=0))

def test_fingerprint_stable_across_key_order():
    assert load_config(a).fingerprint() == load_config(a_reordered).fingerprint()

def test_fingerprint_changes_with_threshold():
    assert load_config(a).fingerprint() != load_config(a_with_other_stop_loss).fingerprint()

def test_execution_defaults_per_market():
    # spec §6.2: el deslizamiento por defecto es mayor en Solana que en CEX
    ex = load_config(minimal_yaml).execution
    assert ex[Market.CEX].slippage_bps == 10 and ex[Market.CEX].fee_bps == 10
    assert ex[Market.SOLANA].slippage_bps == 100 and ex[Market.SOLANA].fee_bps == 25
    assert ex[Market.STOCKS].slippage_bps == 5 and ex[Market.STOCKS].fee_bps == 0
    assert ex[Market.FOMO].slippage_bps == 100 and ex[Market.FOMO].fee_bps == 25

def test_quantity_precision_defaults_per_market():
    ex = load_config(minimal_yaml).execution
    assert (ex[Market.CEX].quantity_precision, ex[Market.SOLANA].quantity_precision) == (8, 6)
    assert (ex[Market.STOCKS].quantity_precision, ex[Market.FOMO].quantity_precision) == (4, 6)

def test_execution_values_overridable_from_yaml():
    assert load_config(yaml_with_cex_slippage(42)).execution[Market.CEX].slippage_bps == 42

def test_example_config_loads_with_conservative_defaults():
    c = load_config(Path("config/config.example.yaml"))
    assert c.mode is Mode.PAPER
    assert c.sizing.flat_amount_usd == Decimal("25")
    assert c.exits.stop_loss_pct == Decimal("15") and c.exits.trailing is False
    assert (c.risk.max_open_positions, c.risk.max_usd_per_token) == (5, Decimal("50"))
    assert c.risk.max_daily_loss_usd == Decimal("50")
    assert c.risk.hard_max_order_usd == Decimal("50")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_config.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `load_config` en `core/config.py` con modelos `pydantic` v2**

Un validador de modelo en `BotConfig` aplica las reglas cruzadas: `stop_loss_pct` obligatorio si `mode == LIVE`; `mirror_pct` y `mirror_cap_usd` obligatorios si `sizing.strategy == "mirror"`; `exits.primary` obligatorio y uno de `copy_sell|take_profit|time_stop`; `take_profit_pct` obligatorio si `primary == "take_profit"`; `time_stop_hours` obligatorio si `primary == "time_stop"`; todos los límites de `risk` positivos. Los errores de `pydantic` se envuelven en `ConfigError` conservando el mensaje. `fingerprint()` serializa con `json.dumps(..., sort_keys=True, default=str)` y devuelve el sha256 hexadecimal.

`execution` se rellena por mercado con los valores por defecto de los tests cuando el YAML no los da, y el YAML puede sobrescribir cualquiera. Los valores viven en configuración y no en el código porque el spec §6.2 exige que el deslizamiento sea ajustable y más alto donde hay menos liquidez.

`config/config.example.yaml` lleva **exactamente** estos valores, elegidos para limitar la pérdida máxima antes que para maximizar la ganancia:

```yaml
mode: paper
sizing:   { strategy: flat, flat_amount_usd: 25 }
exits:    { stop_loss_pct: 15, primary: copy_sell, time_stop_hours: 72, trailing: false }
risk:
  max_usd_per_token:   50
  max_open_positions:  5
  max_usd_per_day:     150
  max_daily_loss_usd:  50
  hard_max_order_usd:  50
cadence:  { entry_cron: "0 * * * *", exit_interval_seconds: 60 }
```

La aritmética que justifica estos números: 5 posiciones × 50 USD de tope por token = 250 USD de exposición máxima simultánea, y el bot se apaga solo al perder 50 en un día. La salida primaria es `copy_sell` porque copiar la salida del trader es lo que significa copy-trading; el stop-loss al 15 % y el time-stop de 72 h son redes de seguridad por debajo, no la estrategia. El trailing va desactivado porque persigue máximos y en activos ruidosos cierra antes de tiempo.

Un test comprueba que el fichero de ejemplo carga sin errores y produce estos valores. `config/.env.example` documenta las claves del spec §7.5, con el comentario de que las de exchange deben crearse **sin permiso de retirada**.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/config.py config/ tests/core/test_config.py
git commit -m "feat(core): añadir configuración validada con reglas cruzadas"
```

---

### Task 3: Esquema SQLite y repositorio

**Files:**
- Create: `storage/db.py`, `storage/repository.py`
- Test: `tests/storage/test_repository.py`

**Interfaces:**
- Consumes: modelos de Tarea 1.
- Produces: `init_db(path: Path) -> sqlite3.Connection` (crea el esquema si no existe, idempotente). `Repository(conn)` con `save_position(Position) -> None`, `get_position(id) -> Position | None`, `open_positions() -> list[Position]`, `close_position(id, exit_price, exit_reason, closed_at) -> None`, `update_peak_price(id, price) -> None`, `save_fill(Fill) -> None`, `fills_between(start, end) -> list[Fill]`, `get_cursor(source: str) -> datetime | None`, `advance_cursor(source: str, ts: datetime) -> None`, `get_frozen_allowlist(session_id) -> frozenset[str] | None`, `freeze_allowlist(session_id, traders) -> None`.

- [ ] **Step 1: Write the failing test**

```python
def test_init_db_is_idempotent():
    init_db(p); init_db(p)  # no debe lanzar

def test_save_and_read_position_preserves_decimal_precision():
    # Decimal("0.000000012345") debe volver idéntico, no como float
    repo.save_position(pos_with(quantity=Decimal("0.000000012345")))
    assert repo.get_position("p1").quantity == Decimal("0.000000012345")

def test_open_positions_excludes_closed():
    repo.save_position(a); repo.save_position(b)
    repo.close_position(b.id, Decimal("90"), ExitReason.STOP_LOSS, T1)
    assert [p.id for p in repo.open_positions()] == [a.id]

def test_cursor_absent_returns_none():
    assert repo.get_cursor("cex") is None

def test_advance_cursor_roundtrip_is_utc():
    repo.advance_cursor("cex", T1)
    assert repo.get_cursor("cex") == T1
    assert repo.get_cursor("cex").tzinfo is timezone.utc

def test_freeze_allowlist_is_write_once_per_session():
    repo.freeze_allowlist("s1", {"t1"})
    with pytest.raises(ValueError):
        repo.freeze_allowlist("s1", {"t1", "t2"})

def test_order_intents_rejects_duplicate_primary_key():
    conn.execute("INSERT INTO order_intents VALUES ('ct-a','RESERVED','{}','2026-11-04',NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_intents VALUES ('ct-a','RESERVED','{}','2026-11-04',NULL)")

def test_audit_log_rejects_update_and_delete():
    conn.execute("INSERT INTO audit_log (timestamp,event,payload) VALUES ('t','e','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET event='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/storage/test_repository.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement el esquema y el repositorio**

**Esta tarea es dueña del esquema completo. Ninguna tarea posterior ejecuta DDL.** Seis tablas:

- `positions`, `fills`, `cursors`, `frozen_allowlist`
- `order_intents`: `client_order_id TEXT PRIMARY KEY`, `status TEXT`, `payload TEXT`, `created_at TEXT`, `error TEXT NULL`. La PRIMARY KEY no es decorativa: la Tarea 8 se apoya en ella para la idempotencia.
- `audit_log`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `timestamp TEXT`, `event TEXT`, `payload TEXT`, más dos triggers `BEFORE UPDATE` y `BEFORE DELETE` que llaman a `RAISE(ABORT, ...)`.

`PRAGMA journal_mode=WAL` y `foreign_keys=ON`.

Los `Decimal` se guardan como `TEXT` y se reconstruyen con `Decimal(str)`; registra adaptadores con `sqlite3.register_adapter`/`register_converter`. Guardarlos como `REAL` perdería precisión, que es justo lo que el spec §2 prohíbe. Las marcas temporales se guardan en ISO-8601 UTC y se devuelven siempre con `tzinfo=timezone.utc`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/storage/test_repository.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add storage/ tests/storage/
git commit -m "feat(storage): añadir esquema SQLite y repositorio"
```

---

### Task 4: Regla de tamaño de posición

**Files:**
- Create: `core/rules/sizing.py`
- Modify: `core/config.py` (añadir la estrategia `risk` y su regla cruzada)
- Test: `tests/core/rules/test_sizing.py`, `tests/core/test_config.py`

**Interfaces:**
- Consumes: `Signal` (Tarea 1), `SizingConfig` (Tarea 2).
- Produces: `calculate_size(signal: Signal, cfg: SizingConfig, ctx: SizingContext) -> SizeResult`. `SizingContext` congelada con `equity_usd: Decimal`, `stop_loss_pct: Decimal | None`, `slippage_bps: int`, `fee_bps: int`. `SizeResult` congelada con `amount_usd: Decimal` y `used_fallback: bool`. `apply_hard_cap(amount_usd: Decimal, hard_max_usd: Decimal) -> Decimal`.
- **Esta tarea añade también la tercera estrategia de tamaño a `core/config.py`**: `SizingConfig.strategy` acepta `risk`, con el campo `risk_pct: Decimal | None`. Regla cruzada nueva: `strategy == "risk"` exige `risk_pct` en (0, 100] **y** exige que `exits.stop_loss_pct` esté configurado. La Tarea 2 está cerrada; esta estrategia entra entera aquí, configuración incluida, para que una sola tarea sea dueña de una sola funcionalidad.

- [ ] **Step 1: Write the failing test**

```python
def test_flat_ignores_trader_amount():
    r = calculate_size(signal_with(trader_amount_usd=Decimal("5000")), flat_cfg(Decimal("50")))
    assert r.amount_usd == Decimal("50") and r.used_fallback is False

def test_mirror_applies_percentage():
    # 10% de 1000 = 100, por debajo del tope de 200
    assert calculate_size(signal_with(Decimal("1000")), mirror_cfg(10, 200)).amount_usd == Decimal("100")

def test_mirror_respects_cap():
    # 10% de 9000 = 900, se recorta al tope 200
    assert calculate_size(signal_with(Decimal("9000")), mirror_cfg(10, 200)).amount_usd == Decimal("200")

def test_mirror_falls_back_to_flat_when_amount_absent():
    # spec §4.3: nunca se adivina un tamaño
    r = calculate_size(signal_with(None), mirror_cfg(10, 200, flat=Decimal("50")))
    assert r.amount_usd == Decimal("50") and r.used_fallback is True

def test_mirror_falls_back_to_flat_when_amount_is_zero():
    # Review Focus 4: cero no debe producir tamaño cero
    r = calculate_size(signal_with(Decimal("0")), mirror_cfg(10, 200, flat=Decimal("50")))
    assert r.amount_usd == Decimal("50") and r.used_fallback is True

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
    # El doble de capital da el doble de importe, salvo un céntimo de redondeo:
    # ROUND_DOWN no se reparte sobre la multiplicación (64.93*2 = 129.86, pero 20/0.154 = 129.87).
    big = calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("2000"))).amount_usd
    small = calculate_size(sig, risk_cfg(pct=1), ctx_with(equity=Decimal("1000"))).amount_usd
    assert abs(big - small * 2) <= Decimal("0.01")

def test_risk_scales_exactly_when_division_is_clean():
    # stop 20%, sin deslizamiento ni comisiones -> fracción 0.2, divide exacto
    ctx = lambda e: SizingContext(equity_usd=e, stop_loss_pct=Decimal("20"), slippage_bps=0, fee_bps=0)
    assert calculate_size(sig, risk_cfg(pct=1), ctx(Decimal("2000"))).amount_usd == \
           calculate_size(sig, risk_cfg(pct=1), ctx(Decimal("1000"))).amount_usd * 2

def test_risk_sizes_smaller_when_stop_is_wider():
    # un stop más lejano exige una posición más pequeña para arriesgar lo mismo
    near = calculate_size(sig, risk_cfg(pct=1), ctx_with(stop=Decimal("5"))).amount_usd
    far  = calculate_size(sig, risk_cfg(pct=1), ctx_with(stop=Decimal("40"))).amount_usd
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

# --- reglas cruzadas nuevas en core/config.py ---

def test_risk_strategy_requires_risk_pct():
    with pytest.raises(ConfigError, match="risk_pct"):
        load_config(yaml_with(strategy="risk", risk_pct=None, stop_loss_pct=15))

def test_risk_strategy_requires_stop_loss():
    with pytest.raises(ConfigError, match="stop_loss_pct"):
        load_config(yaml_with(strategy="risk", risk_pct=1, stop_loss_pct=None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/rules/test_sizing.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `calculate_size` y `apply_hard_cap` en `core/rules/sizing.py`**

Función pura, sin E/S. El redondeo de `mirror` y de `risk` es a 2 decimales con `ROUND_DOWN`: redondear hacia abajo nunca gasta más de lo que la regla permite.

La fórmula de `risk` es la del spec §4.3.1:

```
fracción_estresada = stop_loss_pct/100 + 2*slippage_bps/10000 + 2*fee_bps/10000
amount_usd         = (equity_usd * risk_pct/100) / fracción_estresada
```

El factor 2 en deslizamiento y comisión no es un adorno: hay una pata de entrada y otra de salida, y el stop no se ejecuta en su precio exacto sino algo peor. Omitirlo hace que el riesgo real supere siempre al declarado, que es el fallo que esta estrategia existe para evitar.

`equity_usd` lo obtiene el motor (Tarea 12) de `MarketDataSource.get_balance()`. Con capital cero el importe es cero y no se lanza: es un estado legítimo, no un error.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/rules/test_sizing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/rules/sizing.py tests/core/rules/test_sizing.py
git commit -m "feat(core): añadir regla de tamaño de posición"
```

---

### Task 5: Reglas de salida y su precedencia

**Files:**
- Create: `core/rules/exits.py`
- Test: `tests/core/rules/test_exits.py`

**Interfaces:**
- Consumes: `Position`, `Quote`, `ExitReason` (Tarea 1), `ExitsConfig` (Tarea 2).
- Produces: `evaluate_exit(position: Position, quote: Quote, now: datetime, cfg: ExitsConfig, trader_sold_fraction: Decimal | None) -> ExitDecision | None`. `ExitDecision` congelada con `reason: ExitReason` y `fraction: Decimal` (1 = total). `next_peak_price(position: Position, quote: Quote) -> Decimal`.

- [ ] **Step 1: Write the failing test**

```python
def test_stop_loss_triggers_below_threshold():
    # entrada 100, stop 20%, precio 79 -> cierra
    d = evaluate_exit(pos(entry=100), q(79), T1, cfg(stop_loss_pct=20), None)
    assert d.reason is ExitReason.STOP_LOSS and d.fraction == Decimal("1")

def test_stop_loss_not_triggered_exactly_at_threshold():
    # 80 es exactamente -20%: con desigualdad estricta, no dispara
    assert evaluate_exit(pos(entry=100), q(80), T1, cfg(stop_loss_pct=20), None) is None

def test_copy_sell_partial_mirrors_fraction():
    d = evaluate_exit(pos(entry=100), q(105), T1, cfg(primary="copy_sell"), Decimal("0.5"))
    assert d.reason is ExitReason.COPY_SELL and d.fraction == Decimal("0.5")

def test_take_profit_triggers_above_threshold():
    d = evaluate_exit(pos(entry=100), q(131), T1, cfg(primary="take_profit", tp_pct=30), None)
    assert d.reason is ExitReason.TAKE_PROFIT

def test_trailing_uses_peak_not_entry():
    # pico 200 -> armado, porque +100% supera el tp de 30%.
    # trailing_pct 10 -> cierra por debajo de 200*0.9 = 180.
    d = evaluate_exit(pos(entry=100, peak=200), q(179), T1,
                      cfg(primary="take_profit", tp_pct=30, trailing=True, trailing_pct=10), None)
    assert d.reason is ExitReason.TAKE_PROFIT

def test_trailing_holds_above_the_retrace_threshold():
    # 181 sigue por encima de 180: no se devuelve suficiente, no cierra
    assert evaluate_exit(pos(entry=100, peak=200), q(181), T1,
                         cfg(primary="take_profit", tp_pct=30, trailing=True, trailing_pct=10), None) is None

def test_trailing_not_armed_below_take_profit_threshold():
    # pico 120 = +20%, no alcanza el tp de 30%: el trailing no está armado y no cierra
    assert evaluate_exit(pos(entry=100, peak=120), q(100), T1,
                         cfg(primary="take_profit", tp_pct=30, trailing=True, trailing_pct=10), None) is None

def test_trailing_disabled_by_default():
    assert cfg(primary="take_profit", tp_pct=30).trailing is False

def test_trailing_requires_trailing_pct_in_config():
    with pytest.raises(ConfigError, match="trailing_pct"):
        load_config(yaml_with(primary="take_profit", take_profit_pct=30, trailing=True, trailing_pct=None))

def test_time_stop_triggers_after_hours():
    d = evaluate_exit(pos(opened_at=T0), q(100), T0 + timedelta(hours=49),
                      cfg(primary="time_stop", time_stop_hours=48), None)
    assert d.reason is ExitReason.TIME_STOP

def test_stop_loss_wins_over_copy_sell():
    # spec §4.4: proteger capital precede a todo lo demás
    d = evaluate_exit(pos(entry=100), q(79), T1, cfg(stop_loss_pct=20, primary="copy_sell"), Decimal("1"))
    assert d.reason is ExitReason.STOP_LOSS

def test_copy_sell_wins_over_take_profit():
    d = evaluate_exit(pos(entry=100), q(140), T1,
                      cfg(stop_loss_pct=20, primary="copy_sell", tp_pct=30), Decimal("1"))
    assert d.reason is ExitReason.COPY_SELL

def test_take_profit_wins_over_time_stop():
    d = evaluate_exit(pos(entry=100, opened_at=T0), q(140), T0 + timedelta(hours=49),
                      cfg(primary="take_profit", tp_pct=30, time_stop_hours=48), None)
    assert d.reason is ExitReason.TAKE_PROFIT

def test_time_stop_coexists_with_primary_as_safety_net():
    # primary es copy_sell y el trader no vendió, pero el tiempo venció
    d = evaluate_exit(pos(opened_at=T0), q(100), T0 + timedelta(hours=49),
                      cfg(primary="copy_sell", time_stop_hours=48), None)
    assert d.reason is ExitReason.TIME_STOP

def test_no_exit_when_nothing_triggers():
    assert evaluate_exit(pos(entry=100), q(101), T1, cfg(stop_loss_pct=20, primary="copy_sell"), None) is None

def test_next_peak_price_only_rises():
    assert next_peak_price(pos(peak=200), q(150)) == Decimal("200")
    assert next_peak_price(pos(peak=200), q(250)) == Decimal("250")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/rules/test_exits.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `evaluate_exit` y `next_peak_price` en `core/rules/exits.py`**

Función pura. Evalúa en el orden fijo del spec §4.4 y devuelve en la primera coincidencia: stop-loss, copy-sell, take-profit, time-stop.

**Semántica del take-profit, con y sin trailing.** Son dos números distintos y no deben confundirse:

- `trailing: false` — cierra cuando el precio sube más de `take_profit_pct` sobre la entrada.
- `trailing: true` — `take_profit_pct` pasa a ser el umbral de **armado**: el trailing se activa cuando el **pico** supera ese porcentaje sobre la entrada. Una vez armado, cierra cuando el precio cae más de `trailing_pct` desde el pico. Antes de armarse no cierra nunca por esta vía.

Reutilizar `take_profit_pct` como distancia de retroceso sería un error: con un take-profit del 30 % y pico en 200 desde una entrada de 100, saldrías en 140, devolviendo 60 de los 100 ganados. Son preguntas distintas: cuánto beneficio hace falta para interesarse, y cuánto se está dispuesto a devolver.

**Esta tarea añade `trailing_pct: Decimal | None` a `ExitsConfig`** en `core/config.py`, en el rango (0, 100], con la regla cruzada de que `trailing: true` lo exige. Mismo criterio que la Tarea 4 con `risk_pct`: la Tarea 2 está cerrada y una funcionalidad tiene una sola dueña. Las condiciones 2–4 solo se consideran si coinciden con `cfg.primary`, salvo el time-stop, que actúa también como red de seguridad cuando `time_stop_hours` está configurado. Los umbrales se cruzan con desigualdad estricta.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/rules/test_exits.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/rules/exits.py tests/core/rules/test_exits.py
git commit -m "feat(core): añadir reglas de salida con precedencia fija"
```

---

### Task 6: Gates de riesgo

**Files:**
- Create: `core/rules/risk.py`
- Test: `tests/core/rules/test_risk.py`

**Interfaces:**
- Consumes: `RejectReason` (Tarea 1), `RiskConfig` (Tarea 2).
- Produces: `RiskState` congelada con `utc_day: date`, `spent_usd_today: Decimal`, `realized_pnl_usd_today: Decimal`, `open_position_count: int`, `exposure_by_symbol: Mapping[str, Decimal]`. `evaluate_risk(amount_usd, symbol, state, cfg) -> RiskVerdict`. `RiskVerdict` congelada con `action: RiskAction` (`ALLOW`, `REJECT`, `SHUTDOWN`) y `reason: RejectReason | None`. `build_risk_state(repo, utc_day, exposure_by_symbol, open_position_count) -> RiskState`.

- [ ] **Step 1: Write the failing test**

```python
def test_allows_order_within_all_limits():
    assert evaluate_risk(Decimal("50"), "BTC/USDT", empty_state(), cfg()).action is RiskAction.ALLOW

def test_rejects_when_token_exposure_would_exceed():
    # 80 ya expuesto + 50 = 130 > 100
    v = evaluate_risk(Decimal("50"), "BTC/USDT",
                      state(exposure={"BTC/USDT": Decimal("80")}), cfg(max_usd_per_token=100))
    assert v.action is RiskAction.REJECT and v.reason is RejectReason.MAX_USD_PER_TOKEN

def test_allows_order_exactly_at_token_limit():
    v = evaluate_risk(Decimal("20"), "BTC/USDT",
                      state(exposure={"BTC/USDT": Decimal("80")}), cfg(max_usd_per_token=100))
    assert v.action is RiskAction.ALLOW

def test_rejects_when_max_open_positions_reached():
    v = evaluate_risk(Decimal("50"), "X", state(open_count=5), cfg(max_open_positions=5))
    assert v.reason is RejectReason.MAX_OPEN_POSITIONS

def test_rejects_when_daily_spend_would_exceed():
    v = evaluate_risk(Decimal("60"), "X", state(spent=Decimal("950")), cfg(max_usd_per_day=1000))
    assert v.reason is RejectReason.MAX_USD_PER_DAY

def test_daily_loss_limit_shuts_down_rather_than_rejects():
    # spec §4.5: no rechaza, para
    v = evaluate_risk(Decimal("10"), "X", state(realized=Decimal("-205")), cfg(max_daily_loss_usd=200))
    assert v.action is RiskAction.SHUTDOWN

def test_shutdown_takes_precedence_over_reject():
    v = evaluate_risk(Decimal("9999"), "X", state(realized=Decimal("-205")),
                      cfg(max_daily_loss_usd=200, max_usd_per_day=10))
    assert v.action is RiskAction.SHUTDOWN

def test_build_risk_state_uses_fixed_utc_day_not_now():
    # Review Focus 2: el día se fija al inicio del ciclo, no se vuelve a leer
    repo.save_fill(fill_at(datetime(2026, 11, 4, 23, 59, tzinfo=utc), Decimal("100")))
    repo.save_fill(fill_at(datetime(2026, 11, 5, 0, 1, tzinfo=utc), Decimal("700")))
    assert build_risk_state(repo, date(2026, 11, 4), {}, 0).spent_usd_today == Decimal("100")

def test_build_risk_state_takes_exposure_from_caller():
    # la exposición la agrega Portfolio (Tarea 11), no esta función: una sola agregación
    s = build_risk_state(repo, D, {"BTC/USDT": Decimal("80")}, 3)
    assert s.exposure_by_symbol == {"BTC/USDT": Decimal("80")} and s.open_position_count == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/rules/test_risk.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `evaluate_risk` y `build_risk_state` en `core/rules/risk.py`**

`evaluate_risk` es pura. Comprueba el apagado por pérdida diaria **primero**, antes que cualquier rechazo. Los límites se cruzan con desigualdad estricta: un importe que deja el total exactamente en el límite se permite.

`build_risk_state` recibe el `utc_day` como parámetro en lugar de leer el reloj, y acumula los fills de esa ventana diaria. Esto es lo que hace que un cambio de día a mitad de ciclo no mueva la contabilidad: quien llama fija el día una sola vez.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/rules/test_risk.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/rules/risk.py tests/core/rules/test_risk.py
git commit -m "feat(core): añadir gates de riesgo con apagado por pérdida diaria"
```

---

### Task 7: Allowlist congelada y deduplicación por cursor

**Files:**
- Create: `core/signals.py`
- Test: `tests/core/test_signals.py`

**Interfaces:**
- Consumes: `Signal`, `RejectReason` (Tarea 1), `Repository` (Tarea 3).
- Produces: `ensure_frozen_allowlist(session_id, following, repo) -> frozenset[str]` (devuelve la congelada si ya existe, ignorando `following`). `filter_by_allowlist(signals, allowlist) -> tuple[list[Signal], list[tuple[Signal, RejectReason]]]`. `detect_new_traders(following, frozen) -> set[str]`. `clamp_cursor(candidate, now) -> datetime`.
- Nombre distinto del `Repository.freeze_allowlist` de la Tarea 3 a propósito: el del repositorio escribe sin preguntar, el del núcleo decide si hay que escribir. Llamarlos igual invitaría a usar el equivocado.

- [ ] **Step 1: Write the failing test**

```python
def test_ensure_frozen_allowlist_persists_first_value():
    assert ensure_frozen_allowlist("s1", {"t1", "t2"}, repo) == frozenset({"t1", "t2"})

def test_second_freeze_in_same_session_returns_original():
    # spec §4.1: un trader añadido a mitad de sesión NO se copia
    ensure_frozen_allowlist("s1", {"t1"}, repo)
    assert ensure_frozen_allowlist("s1", {"t1", "t2"}, repo) == frozenset({"t1"})

def test_new_session_can_freeze_different_allowlist():
    ensure_frozen_allowlist("s1", {"t1"}, repo)
    assert ensure_frozen_allowlist("s2", {"t1", "t2"}, repo) == frozenset({"t1", "t2"})

def test_filter_rejects_signals_from_unknown_trader():
    accepted, rejected = filter_by_allowlist([sig("t1"), sig("t9")], frozenset({"t1"}))
    assert [s.trader_id for s in accepted] == ["t1"]
    assert rejected == [(sig("t9"), RejectReason.NOT_IN_ALLOWLIST)]

def test_detect_new_traders_reports_additions_only():
    assert detect_new_traders({"t1", "t2"}, frozenset({"t1"})) == {"t2"}
    assert detect_new_traders({"t1"}, frozenset({"t1", "t2"})) == set()

def test_clamp_cursor_rejects_future_timestamp():
    # Review Focus 5: reloj de la fuente adelantado no debe saltarse señales reales
    assert clamp_cursor(NOW + timedelta(hours=5), NOW) == NOW

def test_clamp_cursor_keeps_past_timestamp():
    assert clamp_cursor(NOW - timedelta(minutes=5), NOW) == NOW - timedelta(minutes=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_signals.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement las cuatro funciones en `core/signals.py`**

`ensure_frozen_allowlist` consulta `repo.get_frozen_allowlist(session_id)` y solo escribe si no hay nada; es el único que debe llamar el motor, nunca `Repository.freeze_allowlist` directamente. `clamp_cursor` devuelve `min(candidate, now)`. Las otras dos son puras.

El `Repository` es el único contacto con E/S y entra por parámetro, de modo que los tests usan un repositorio en memoria sin tocar la capa de adaptadores.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_signals.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/signals.py tests/core/test_signals.py
git commit -m "feat(core): añadir allowlist congelada y recorte de cursor"
```

---

### Task 8: Idempotencia de órdenes

**Files:**
- Create: `core/idempotency.py`
- Modify: `storage/repository.py` (añadir los métodos de `order_intents`)
- Test: `tests/core/test_idempotency.py`

**Interfaces:**
- Consumes: `Side`, `Signal` (Tarea 1), `Repository` (Tarea 3).
- Produces: `client_order_id(signal_id, trader_id, symbol, side, cycle_ts) -> str`. `IntentStatus` (`RESERVED`, `SENT`, `CONFIRMED`, `FAILED`). Dataclass congelada `OrderIntent` con `client_order_id`, `status: IntentStatus`, `payload: dict`, `created_at`, `error: str | None`. En `Repository`: `reserve_intent(coid, payload) -> bool`, `mark_intent_sent(coid)`, `mark_intent_confirmed(coid, fill)`, `mark_intent_failed(coid, error)`, `unconfirmed_intents() -> list[OrderIntent]`.
- Nota: `unconfirmed_intents()` se construye aquí pero no tiene consumidor en este plan. La reconciliación al arrancar (spec §4.7) pertenece al plan 3, junto a la ejecución real; sin órdenes reales no hay respuestas ambiguas que reconciliar. Se implementa ahora porque el esquema y la máquina de estados deben existir antes de que haya algo que reconciliar.

- [ ] **Step 1: Write the failing test**

```python
def test_client_order_id_is_deterministic():
    assert client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0) == \
           client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0)

def test_client_order_id_differs_per_field():
    base = client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0)
    assert base != client_order_id("s2", "t1", "BTC/USDT", Side.BUY, T0)
    assert base != client_order_id("s1", "t1", "BTC/USDT", Side.SELL, T0)
    assert base != client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T1)

def test_client_order_id_fits_exchange_limit():
    # ccxt y Binance limitan el clientOrderId a 36 caracteres
    assert len(client_order_id("s1", "t1", "BTC/USDT", Side.BUY, T0)) <= 36

def test_reserve_intent_returns_true_first_time():
    assert repo.reserve_intent(coid, payload) is True

def test_reserve_intent_returns_false_on_duplicate():
    # spec §4.7: la misma señal reprocesada no genera una segunda orden
    repo.reserve_intent(coid, payload)
    assert repo.reserve_intent(coid, payload) is False

def test_unconfirmed_intents_lists_reserved_and_sent_only():
    repo.reserve_intent(c1, p); repo.reserve_intent(c2, p); repo.mark_intent_sent(c2)
    repo.reserve_intent(c3, p); repo.mark_intent_confirmed(c3, fill)
    repo.reserve_intent(c4, p); repo.mark_intent_failed(c4, "rechazada")
    assert {i.client_order_id for i in repo.unconfirmed_intents()} == {c1, c2}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_idempotency.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `client_order_id` y los métodos de intents**

`client_order_id` concatena los campos con un separador que no puede aparecer en ellos, calcula sha256 y devuelve los primeros 32 caracteres hexadecimales con el prefijo `ct-`. 36 caracteres en total, el máximo que aceptan Binance y la mayoría de CEX vía `ccxt`.

`reserve_intent` se apoya en la restricción `PRIMARY KEY` de `order_intents`: intenta el `INSERT` y devuelve `False` al capturar `sqlite3.IntegrityError`. Comprobar antes con un `SELECT` dejaría una ventana de carrera entre la comprobación y la inserción; la restricción de la base de datos no la deja.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_idempotency.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/idempotency.py storage/repository.py tests/core/test_idempotency.py
git commit -m "feat(core): añadir idempotencia de órdenes por client_order_id"
```

---

### Task 9: Log de auditoría append-only

**Files:**
- Create: `storage/audit.py`
- Test: `tests/storage/test_audit.py`

**Interfaces:**
- Consumes: `Repository` (Tarea 3).
- Produces: `AuditLog(conn)` con `record(event: str, payload: dict) -> int` (devuelve el id de la fila) y `events(since=None, event=None) -> list[AuditEvent]`. `AuditEvent` congelada con `id`, `timestamp`, `event`, `payload`.

- [ ] **Step 1: Write the failing test**

```python
def test_record_returns_increasing_ids():
    assert audit.record("signal_rejected", {}) < audit.record("order_sent", {})

def test_payload_roundtrips_as_json():
    audit.record("order_sent", {"amount_usd": "50.00", "symbol": "BTC/USDT"})
    assert audit.events()[0].payload["amount_usd"] == "50.00"

def test_events_filter_by_type():
    audit.record("a", {}); audit.record("b", {})
    assert [e.event for e in audit.events(event="b")] == ["b"]

def test_update_is_rejected():
    # spec §7.8: solo se inserta, nunca se actualiza ni se borra
    rid = audit.record("x", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET event='y' WHERE id=?", (rid,))

def test_delete_is_rejected():
    rid = audit.record("x", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id=?", (rid,))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/storage/test_audit.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `AuditLog` en `storage/audit.py`**

El `payload` se serializa con `json.dumps(..., default=str)` para que los `Decimal` y las fechas sobrevivan como texto.

La inmutabilidad se impone con dos triggers SQLite, `BEFORE UPDATE` y `BEFORE DELETE` sobre `audit_log`, que llaman a `RAISE(ABORT, ...)`. Depender de que el código de la aplicación no actualice la tabla no es una garantía; un trigger sí lo es, y es lo que pide el spec §7.8.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/storage/test_audit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add storage/audit.py tests/storage/test_audit.py
git commit -m "feat(storage): añadir log de auditoría inmutable"
```

---

### Task 10: Puertos y candado de modo

**Files:**
- Create: `ports/market.py`, `ports/signal_source.py`, `ports/notifier.py`, `core/safety.py`
- Test: `tests/core/test_safety.py`, `tests/test_architecture.py`

**Interfaces:**
- Consumes: modelos de Tarea 1.
- Produces: ABC `MarketDataSource` (`get_quote(symbol) -> Quote`, `get_balance() -> Decimal`), ABC `OrderExecutor` (`submit_buy(order) -> Fill`, `submit_sell(order) -> Fill`), ABC `SignalSource` (`fetch_trades(since) -> list[Signal]`, `list_followed() -> list[str]`, `name: str`), ABC `Notifier` (`send(message, level) -> None`). Excepciones `QuoteUnavailable` y `ZeroQuantity` en `ports/market.py`, y enum `Level` (`INFO`, `WARNING`, `CRITICAL`) en `ports/notifier.py`. En `core/safety.py`: `mode_prefix(mode) -> str`, `is_kill_switch_active(root: Path) -> bool`, `CircuitBreaker(threshold=3)` con `record_failure()`, `record_success()`, `is_open`.

- [ ] **Step 1: Write the failing test**

```python
def test_mode_prefix_declares_mode():
    assert mode_prefix(Mode.PAPER) == "mode: paper"
    assert mode_prefix(Mode.LIVE) == "mode: live"

def test_kill_switch_detects_stop_file():
    assert is_kill_switch_active(tmp_path) is False
    (tmp_path / "STOP").touch()
    assert is_kill_switch_active(tmp_path) is True

def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3)
    for _ in range(2): cb.record_failure()
    assert cb.is_open is False
    cb.record_failure()
    assert cb.is_open is True

def test_circuit_breaker_resets_on_success():
    # spec §4.8: el contador es de fallos CONSECUTIVOS
    cb = CircuitBreaker(threshold=3)
    cb.record_failure(); cb.record_failure(); cb.record_success(); cb.record_failure()
    assert cb.is_open is False

def test_core_does_not_import_adapters():
    # spec §3.2: las dependencias apuntan siempre hacia dentro
    for py in Path("core").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            for target in filter(None, [mod, *names]):
                assert not target.startswith(("adapters", "interfaces", "backtest")), f"{py}: {target}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_safety.py tests/test_architecture.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement los cuatro puertos como ABC y `core/safety.py`**

Los puertos solo declaran firmas con `@abstractmethod`, sin implementación. `MarketDataSource` y `OrderExecutor` van separados a propósito (spec §3.3): el modo simulado combina la fuente de datos real de un mercado con el ejecutor de `paper/`.

`QuoteUnavailable` vive en `ports/`, no en `adapters/cex/`. Tiene que ser así: el motor necesita capturarla y `core/` no puede importar de `adapters/` (spec §3.2). Una excepción definida en el adaptador sería inalcanzable para quien debe manejarla.

El test de arquitectura recorre `core/` con `ast` y falla si encuentra un import hacia fuera. Es la única forma de que la regla de dependencia no se erosione con el tiempo.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_safety.py tests/test_architecture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ports/ core/safety.py tests/core/test_safety.py tests/test_architecture.py
git commit -m "feat(ports): añadir puertos, candado de modo y test de arquitectura"
```

---

### Task 11: Cartera y PnL

**Files:**
- Create: `core/portfolio.py`
- Test: `tests/core/test_portfolio.py`

**Interfaces:**
- Consumes: `Position`, `Fill`, `Quote` (Tarea 1), `Repository` (Tarea 3).
- Produces: `Portfolio(repo)` con `open_positions() -> list[Position]`, `exposure_by_symbol() -> dict[str, Decimal]`, `unrealized_pnl_usd(quotes: Mapping[str, Quote]) -> Decimal`, `realized_pnl_usd(day: date) -> Decimal`, `position_for_symbol(symbol) -> Position | None`.

- [ ] **Step 1: Write the failing test**

```python
def test_exposure_aggregates_positions_in_same_symbol():
    assert Portfolio(repo_with(pos("BTC/USDT", 100), pos("BTC/USDT", 50), pos("ETH/USDT", 70))) \
        .exposure_by_symbol() == {"BTC/USDT": Decimal("150"), "ETH/USDT": Decimal("70")}

def test_unrealized_pnl_sums_open_positions():
    # entrada 100 x2 uds, precio 110 -> +20
    assert Portfolio(repo_with(pos(entry=100, qty=2))) \
        .unrealized_pnl_usd({"BTC/USDT": q(110)}) == Decimal("20")

def test_unrealized_pnl_skips_symbol_without_quote():
    # Review Focus 1: falta de precio no debe romper el cálculo global
    assert Portfolio(repo_with(pos("BTC/USDT"), pos("MISSING"))) \
        .unrealized_pnl_usd({"BTC/USDT": q(110)}) == Decimal("20")

def test_realized_pnl_counts_only_requested_day():
    assert Portfolio(repo_with_fills_on_two_days()).realized_pnl_usd(date(2026, 11, 4)) == Decimal("30")

def test_realized_pnl_includes_fees():
    # venta con 5 de comisión reduce el PnL realizado
    assert Portfolio(repo_with(sell_fill(pnl=50, fee=5))).realized_pnl_usd(D) == Decimal("45")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_portfolio.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `Portfolio` en `core/portfolio.py`**

Lee del `Repository`, no mantiene estado propio: la base de datos es la fuente de verdad, de modo que un reinicio no pierde nada. Un símbolo sin cotización se omite del PnL latente y no lanza.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_portfolio.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/portfolio.py tests/core/test_portfolio.py
git commit -m "feat(core): añadir cartera y cálculo de PnL"
```

---

### Task 12: Motor de decisión

**Files:**
- Create: `core/engine.py`
- Test: `tests/core/test_engine.py`

**Interfaces:**
- Consumes: todo lo anterior. Recibe por constructor `config`, `repo`, `audit`, `portfolio`, `data_source`, `executor`, `notifier`, `circuit_breaker`, `project_root`.
- Produces: `Engine` con `run_entry_cycle(source: SignalSource, now: datetime) -> EntryCycleResult` y `run_exit_cycle(now: datetime) -> ExitCycleResult`. `EntryCycleResult` congelada con `opened: list[Position]`, `rejected: list[tuple[Signal, RejectReason]]`, `cursor_advanced: bool`, `shutdown: bool`. `ExitCycleResult` con `closed: list[Position]`, `errors: list[str]`.

- [ ] **Step 1: Write the failing test**

```python
def test_entry_cycle_opens_position_and_advances_cursor():
    r = engine.run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert len(r.opened) == 1 and r.cursor_advanced is True

def test_cursor_not_advanced_when_source_raises():
    # spec §4.1: el cursor avanza SOLO si el ciclo termina sin excepción
    r = engine.run_entry_cycle(RaisingSource(), NOW)
    assert r.cursor_advanced is False and repo.get_cursor("fake") is None

def test_reprocessed_cycle_does_not_duplicate_position():
    # spec §4.7: reprocesar tras una caída es inocuo
    engine.run_entry_cycle(FakeSource([sig("t1")]), NOW)
    r = engine.run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert r.opened == [] and len(repo.open_positions()) == 1

def test_invalid_quote_rejects_without_opening():
    # Review Focus 1
    r = engine_with_quote(Decimal("0")).run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert r.opened == [] and r.rejected[0][1] is RejectReason.INVALID_QUOTE

def test_negative_quote_rejects_without_opening():
    r = engine_with_quote(Decimal("-5")).run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert r.rejected[0][1] is RejectReason.INVALID_QUOTE

def test_quantity_rounding_to_zero_rejects():
    # Review Focus 3: 50 USD de un activo a 120000 con 4 decimales redondea a 0
    r = engine_with(quote=Decimal("120000"), amount=Decimal("50"), precision=4) \
        .run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert r.rejected[0][1] is RejectReason.ZERO_QUANTITY

def test_risk_shutdown_stops_cycle_and_reports():
    r = engine_with_daily_loss_exceeded().run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert r.shutdown is True and r.opened == []

def test_kill_switch_blocks_entries():
    (root / "STOP").touch()
    assert engine.run_entry_cycle(FakeSource([sig("t1")]), NOW).opened == []

def test_kill_switch_does_not_block_exits():
    # spec §4.6: las salidas siguen para no dejar posiciones huérfanas
    (root / "STOP").touch()
    assert len(engine_with_open_position_below_stop().run_exit_cycle(NOW).closed) == 1

def test_hard_cap_applied_before_order():
    r = engine_with(flat=Decimal("9999"), hard_max=Decimal("500")).run_entry_cycle(src, NOW)
    assert r.opened[0].amount_usd == Decimal("500")

def test_exit_cycle_closes_position_on_stop_loss():
    c = engine_with_open_position_below_stop().run_exit_cycle(NOW).closed
    assert c[0].exit_reason is ExitReason.STOP_LOSS

def test_exit_cycle_updates_peak_price_when_rising():
    engine_with_open_position(entry=100, peak=100, quote=150).run_exit_cycle(NOW)
    assert repo.get_position("p1").peak_price == Decimal("150")

def test_exit_cycle_survives_missing_quote():
    # spec §8: mantiene la posición y reintenta, no cierra a ciegas
    r = engine_with_open_position_and_failing_quote().run_exit_cycle(NOW)
    assert r.closed == [] and len(r.errors) == 1

def test_circuit_breaker_opens_after_three_failed_orders():
    for _ in range(3): engine_with_failing_executor().run_entry_cycle(src, NOW)
    assert engine.circuit_breaker.is_open is True

def test_every_decision_is_audited():
    engine.run_entry_cycle(FakeSource([sig("t1")]), NOW)
    assert {e.event for e in audit.events()} >= {"signal_accepted", "order_reserved", "position_opened"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_engine.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `Engine` en `core/engine.py`**

`run_entry_cycle`, en este orden: comprobar kill switch; fijar `utc_day = now.date()` **una sola vez**; congelar allowlist con `ensure_frozen_allowlist`; leer el cursor y pedir señales; filtrar por allowlist; para cada señal, obtener cotización y validarla (`> 0`, no `None`); construir el estado de riesgo con la exposición y el recuento que da `Portfolio`, y evaluarlo (si `SHUTDOWN`, terminar el ciclo); calcular tamaño y aplicar el techo duro; aplicar el **guardia de cantidad cero**; calcular `client_order_id` y reservar el intent (si devuelve `False`, omitir como duplicado); enviar al ejecutor; registrar posición y fill; auditar cada paso. El cursor avanza con `clamp_cursor` solo al final, si no hubo excepción.

**El guardia de cantidad cero no calcula la cantidad definitiva.** Divide el importe entre el precio cotizado, redondea con el `quantity_precision` del mercado y rechaza con `ZERO_QUANTITY` si da cero. La cantidad autoritativa la produce el ejecutor desde el precio ya deslizado, y `Position.quantity` sale siempre del `Fill`, nunca de este guardia. El guardia existe para rechazar **antes** de consumir un `client_order_id`; si aun así el ejecutor lanza `ZeroQuantity`, el motor lo trata como orden fallida y marca el intent como fallido.

La reserva del intent va **después** de los rechazos, no antes: un `client_order_id` reservado es irrepetible, y gastarlo en una señal que el riesgo iba a rechazar impediría reintentarla cuando las condiciones cambien.

`run_exit_cycle`: para cada posición abierta, obtener cotización (si falla, acumular en `errors` y continuar con la siguiente), actualizar `peak_price` con `next_peak_price`, evaluar salida y cerrar si procede. No comprueba el kill switch.

El `Engine` recibe todas sus dependencias por constructor y no construye ninguna: es lo que permite que el backtest (Tarea 14) lo use sin cambios.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/engine.py tests/core/test_engine.py
git commit -m "feat(core): añadir motor de decisión de entrada y salida"
```

---

### Task 13: Ejecutor simulado

**Files:**
- Create: `adapters/paper/executor.py`
- Test: `tests/adapters/paper/test_executor.py`

**Interfaces:**
- Consumes: `OrderExecutor` (Tarea 10), `MarketDataSource` (Tarea 10), `Order`, `Fill` (Tarea 1).
- Produces: `PaperExecutor(data_source, slippage_bps: int, fee_bps: int, quantity_precision: int)` implementando `OrderExecutor`. Los tres enteros salen de `config.execution[market]` (Tarea 2); el ejecutor no los fija por su cuenta.

- [ ] **Step 1: Write the failing test**

```python
def test_buy_fill_price_includes_slippage_against_the_buyer():
    # 100 con 50 bps -> 100.50: el deslizamiento siempre juega en contra
    assert PaperExecutor(src(100), slippage_bps=50, fee_bps=0) \
        .submit_buy(order(Decimal("100"))).price == Decimal("100.50")

def test_sell_fill_price_includes_slippage_against_the_seller():
    assert PaperExecutor(src(100), slippage_bps=50, fee_bps=0) \
        .submit_sell(sell_order()).price == Decimal("99.50")

def test_fee_is_charged_on_notional():
    # 10 bps de 100 USD = 0.10
    assert PaperExecutor(src(100), 0, fee_bps=10).submit_buy(order(Decimal("100"))).fee_usd == Decimal("0.10")

def test_quantity_derived_from_slipped_price_not_quoted_price():
    f = PaperExecutor(src(100), slippage_bps=50, fee_bps=0).submit_buy(order(Decimal("100.50")))
    assert f.quantity == Decimal("1")

def test_fill_carries_client_order_id():
    assert PaperExecutor(src(100), 0, 0).submit_buy(order(coid="ct-abc")).client_order_id == "ct-abc"

def test_uses_real_quote_from_data_source():
    # spec §3.3: el dinero es falso, los precios no
    assert PaperExecutor(src(42), 0, 0, 8).submit_buy(order(Decimal("42"))).price == Decimal("42")

def test_raises_when_quantity_rounds_to_zero():
    # el ejecutor es la autoridad sobre la cantidad: si el precio deslizado la anula, falla
    with pytest.raises(ZeroQuantity):
        PaperExecutor(src(120000), 0, 0, quantity_precision=4).submit_buy(order(Decimal("50")))

def test_quantity_respects_configured_precision():
    f = PaperExecutor(src(3), 0, 0, quantity_precision=2).submit_buy(order(Decimal("10")))
    assert f.quantity == Decimal("3.33")  # ROUND_DOWN, nunca compra de más
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/adapters/paper/test_executor.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `PaperExecutor`**

Pide la cotización real al `MarketDataSource` que recibe por constructor, aplica el deslizamiento en la dirección que perjudica al operador (sumando en compras, restando en ventas), calcula la comisión sobre el nocional y devuelve un `Fill`.

La cantidad se redondea con `ROUND_DOWN` a `quantity_precision` decimales, y si da cero se lanza `ZeroQuantity` en lugar de devolver un `Fill` vacío. Este ejecutor es la autoridad sobre la cantidad: el guardia del motor (Tarea 12) solo rechaza antes de reservar el intent, y la cantidad que se persiste en la posición sale siempre de este `Fill`.

El deslizamiento va siempre en contra a propósito: un simulador optimista convierte estrategias perdedoras en ganadoras sobre el papel, que es exactamente lo que el spec §6.2 quiere evitar.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/adapters/paper/test_executor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add adapters/paper/ tests/adapters/paper/
git commit -m "feat(adapters): añadir ejecutor simulado con deslizamiento y comisiones"
```

---

### Task 14: Backtest y métricas

**Files:**
- Create: `backtest/runner.py`, `backtest/metrics.py`
- Test: `tests/backtest/test_runner.py`, `tests/backtest/test_metrics.py`

**Interfaces:**
- Consumes: `Engine` (Tarea 12), `PaperExecutor` (Tarea 13), `SignalSource` y `MarketDataSource` (Tarea 10).
- Produces: `HistoricalSource(signals, quotes)` implementando ambos puertos de lectura. `run_backtest(config, signals, quotes, start, end, step) -> BacktestResult`. `BacktestResult` congelada con `metrics: Metrics`, `positions: list[Position]`, `fills: list[Fill]`, `engine_class: type`. `compute_metrics(fills, positions) -> Metrics` congelada con `total_pnl_usd`, `max_drawdown_usd`, `win_rate`, `expectancy_usd`, `sharpe`, `avg_duration_hours`, `closes_by_reason: dict[ExitReason, int]`.

- [ ] **Step 1: Write the failing test**

```python
def test_backtest_uses_the_same_engine_class():
    # spec §6.1: una divergencia entre backtest y vivo sería un defecto
    assert run_backtest(...).engine_class is Engine

def test_backtest_is_deterministic():
    assert run_backtest(cfg, sigs, quotes, S, E, STEP).metrics == \
           run_backtest(cfg, sigs, quotes, S, E, STEP).metrics

def test_backtest_opens_and_closes_across_time_steps():
    r = run_backtest(cfg(stop_loss_pct=20), [sig_at(T0)], falling_quotes(), T0, T0 + timedelta(hours=3), STEP)
    assert r.metrics.closes_by_reason[ExitReason.STOP_LOSS] == 1

def test_win_rate_counts_closed_positions_only():
    assert compute_metrics(fills_2_wins_1_loss_1_open(), positions).win_rate == Decimal("0.6667")

def test_max_drawdown_measures_peak_to_trough():
    # curva 0 -> +100 -> -50 -> +20 : caída máxima 150
    assert compute_metrics(curve_fills(), positions).max_drawdown_usd == Decimal("150")

def test_expectancy_is_mean_pnl_per_closed_trade():
    assert compute_metrics(fills_pnl(30, -10, 20), positions).expectancy_usd == Decimal("13.3333")

def test_closes_by_reason_breaks_down_all_reasons():
    # spec §6.3: es la métrica que dice si los umbrales están calibrados
    assert compute_metrics(mixed_fills(), positions).closes_by_reason == \
        {ExitReason.STOP_LOSS: 2, ExitReason.TAKE_PROFIT: 1, ExitReason.TIME_STOP: 3}

def test_metrics_on_empty_input_do_not_raise():
    m = compute_metrics([], [])
    assert m.total_pnl_usd == Decimal("0") and m.win_rate == Decimal("0") and m.sharpe == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/backtest/ -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `run_backtest` y `compute_metrics`**

`run_backtest` construye un `Engine` real con `HistoricalSource` y `PaperExecutor`, y avanza el reloj en pasos de `step` desde `start` hasta `end`, llamando a `run_entry_cycle` y `run_exit_cycle` en cada paso con ese `now` simulado. Esto es lo que garantiza que backtest y operación en vivo compartan motor.

**`compute_metrics` delega en `quantstats` (0.0.86, ya instalado) todo lo que sea estadística estándar**: drawdown máximo, Sharpe, Sortino, volatilidad, win rate, expectancy. Construye una serie de retornos a partir de los fills y se la pasa.

Razón para no calcularlo a mano: el Sharpe anualizado, el drawdown sobre una curva con aportaciones y la expectancy tienen convenios sutiles que es fácil equivocar en silencio, y un número de rendimiento equivocado es peor que no tenerlo, porque se actúa sobre él. `quantstats` además genera un informe HTML completo con `qs.reports.html(...)`, que la CLI expone como `backtest --report <fichero>`.

**Lo que NO se delega: `closes_by_reason`.** Esa métrica es propia de este bot y es la que dice si los umbrales están calibrados; se calcula aquí y se añade al informe.

Toda división comprueba antes el divisor: con cero operaciones cerradas las métricas valen cero en lugar de lanzar, y ese camino se prueba **sin** llamar a `quantstats`, porque una serie vacía le hace devolver `NaN`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/backtest/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backtest/ tests/backtest/
git commit -m "feat(backtest): añadir replay histórico y métricas"
```

---

### Task 15: Fuente de datos de CEX

**Files:**
- Create: `adapters/cex/source.py`
- Test: `tests/adapters/cex/test_source.py`

**Interfaces:**
- Consumes: `MarketDataSource`, `SignalSource` (Tarea 10).
- Produces: `CexDataSource(exchange_id: str, api_key=None, secret=None, testnet=False)` implementando `MarketDataSource`. `CexCsvSignalSource(csv_path, market=Market.CEX)` implementando `SignalSource`. Excepción `QuoteUnavailable`.

- [ ] **Step 1: Write the failing test**

```python
def test_get_quote_returns_decimal_from_last_price():
    # el precio NUNCA debe pasar por float: 0.1 + 0.2 no es 0.3
    assert CexDataSource.from_client(fake_ccxt({"last": "0.000000012345"})) \
        .get_quote("X/USDT").price == Decimal("0.000000012345")

def test_get_quote_raises_on_zero_price():
    # Review Focus 1
    with pytest.raises(QuoteUnavailable):
        CexDataSource.from_client(fake_ccxt({"last": "0"})).get_quote("X/USDT")

def test_get_quote_raises_on_missing_last_price():
    with pytest.raises(QuoteUnavailable):
        CexDataSource.from_client(fake_ccxt({"last": None})).get_quote("X/USDT")

def test_get_quote_wraps_network_error():
    with pytest.raises(QuoteUnavailable):
        CexDataSource.from_client(raising_ccxt(ccxt.NetworkError)).get_quote("X/USDT")

def test_testnet_flag_enables_sandbox_mode():
    assert fake_client_built_with(testnet=True).sandbox_enabled is True

def test_csv_source_parses_signals_after_cursor():
    sigs = CexCsvSignalSource(csv_with_rows_at(T0, T1)).fetch_trades(since=T0)
    assert [s.timestamp for s in sigs] == [T1]

def test_csv_source_signal_ids_are_stable_across_reads():
    # el id de la señal alimenta el client_order_id: debe ser reproducible
    src = CexCsvSignalSource(path)
    assert [s.id for s in src.fetch_trades(None)] == [s.id for s in src.fetch_trades(None)]

def test_rate_limit_retries_with_backoff_then_succeeds():
    # spec §8: backoff exponencial con tope
    client = ccxt_failing_times(2, ccxt.RateLimitExceeded, then={"last": "100"})
    assert CexDataSource.from_client(client).get_quote("X/USDT").price == Decimal("100")
    assert client.call_count == 3

def test_rate_limit_gives_up_after_max_retries():
    with pytest.raises(QuoteUnavailable):
        CexDataSource.from_client(ccxt_always_raising(ccxt.RateLimitExceeded)).get_quote("X/USDT")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/adapters/cex/test_source.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `CexDataSource` y `CexCsvSignalSource`**

`CexDataSource` envuelve `ccxt`, llamando a `fetch_ticker`. El cliente de `ccxt` se inyecta por un constructor alternativo `from_client` para que los tests no toquen la red. Los precios se convierten con `Decimal(str(valor))`, nunca `Decimal(float)`. Cualquier error de `ccxt` o precio no positivo se envuelve en `QuoteUnavailable` (definida en `ports/market.py`, Tarea 10), de forma que el motor lo trate con el camino de `INVALID_QUOTE` ya probado en la Tarea 12.

`RateLimitExceeded` se reintenta con backoff exponencial, 3 intentos y tope de 8 segundos; agotados los intentos, se convierte también en `QuoteUnavailable`. El reintento se limita a los errores de límite de tasa: reintentar un símbolo inexistente solo gasta tiempo.

`CexCsvSignalSource` lee un CSV con columnas `timestamp,trader_id,symbol,side,amount_usd` y deriva el `id` de cada señal por hash de la fila completa, para que sea estable entre lecturas.

El `SignalSource` de la API de copy-trading del exchange queda para el plan 4; el CSV cubre el flujo completo y es lo que permite backtestear desde ya.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/adapters/cex/test_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add adapters/cex/ tests/adapters/cex/
git commit -m "feat(adapters): añadir fuente de precios y señales de CEX"
```

---

### Task 16: Notificador de Telegram

**Files:**
- Create: `adapters/notify/telegram.py`, `adapters/notify/console.py`
- Test: `tests/adapters/notify/test_telegram.py`

**Interfaces:**
- Consumes: `Notifier` (Tarea 10), `mode_prefix` (Tarea 10).
- Produces: `TelegramNotifier(token, chat_id, mode, client=None)` y `ConsoleNotifier(mode)`, ambos implementando `Notifier`.

- [ ] **Step 1: Write the failing test**

```python
def test_message_is_prefixed_with_mode():
    # spec §7.1: TODO mensaje declara el modo
    TelegramNotifier("t", "1", Mode.PAPER, client=fake).send("abierta BTC", Level.INFO)
    assert fake.last_text.startswith("mode: paper")

def test_live_mode_prefix_differs():
    TelegramNotifier("t", "1", Mode.LIVE, client=fake).send("abierta BTC", Level.INFO)
    assert fake.last_text.startswith("mode: live")

def test_posts_to_send_message_endpoint_with_chat_id():
    TelegramNotifier("tok", "42", Mode.PAPER, client=fake).send("x", Level.INFO)
    assert fake.last_url.endswith("/bottok/sendMessage") and fake.last_json["chat_id"] == "42"

def test_network_error_does_not_propagate():
    # una notificación caída no puede tumbar un ciclo de trading
    TelegramNotifier("t", "1", Mode.PAPER, client=raising).send("x", Level.INFO)

def test_console_notifier_also_prefixes_mode(capsys):
    ConsoleNotifier(Mode.PAPER).send("x", Level.INFO)
    assert capsys.readouterr().out.startswith("mode: paper")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/adapters/notify/test_telegram.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement los dos notificadores**

`TelegramNotifier` hace `POST` a `https://api.telegram.org/bot<token>/sendMessage` con `httpx`, inyectable por constructor para los tests. Antepone `mode_prefix(mode)` a todo mensaje.

Captura y registra cualquier excepción de red sin volver a lanzarla: si Telegram está caído, el bot debe seguir gestionando posiciones. Una notificación perdida es un inconveniente; un ciclo de salida abortado por un fallo de notificación deja posiciones sin vigilar.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/adapters/notify/test_telegram.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add adapters/notify/ tests/adapters/notify/
git commit -m "feat(adapters): añadir notificadores de Telegram y consola"
```

---

### Task 17: CLI y arranque

**Files:**
- Create: `cli.py`, `pyproject.toml`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: comandos `run`, `backtest`, `status`, `positions`, `stop` vía `typer`. `build_engine(config, root) -> Engine` (compone las dependencias concretas).

- [ ] **Step 1: Write the failing test**

```python
def test_run_refuses_live_mode_in_this_plan():
    # el plan 3 añade la ejecución real; aquí debe negarse explícitamente
    r = CliRunner().invoke(app, ["run", "--config", live_yaml])
    assert r.exit_code != 0 and "no implementado" in r.output

def test_run_announces_paper_mode():
    assert "mode: paper" in CliRunner().invoke(app, ["run", "--config", paper_yaml, "--once"]).output

def test_stop_creates_kill_switch_file():
    CliRunner().invoke(app, ["stop"])
    assert (root / "STOP").exists()

def test_status_reports_mode_and_open_positions():
    out = CliRunner().invoke(app, ["status", "--config", paper_yaml]).output
    assert "mode: paper" in out

def test_backtest_prints_closes_by_reason():
    out = CliRunner().invoke(app, ["backtest", "--config", paper_yaml, "--signals", csv]).output
    assert "STOP_LOSS" in out

def test_invalid_config_exits_with_message_not_traceback():
    r = CliRunner().invoke(app, ["status", "--config", broken_yaml])
    assert r.exit_code != 0 and "Traceback" not in r.output

def test_run_loop_uses_configured_cadence():
    # spec §4.2: dos bucles independientes, entrada horaria y salida cada 60 s
    sched = fake_scheduler()
    CliRunner().invoke(app, ["run", "--config", paper_yaml, "--once"], obj=sched)
    assert sched.entry_interval == timedelta(hours=1)
    assert sched.exit_interval == timedelta(seconds=60)

def test_exit_loop_runs_more_often_than_entry_loop():
    # un stop-loss comprobado una vez por hora no es un stop-loss
    cfg = load_config(paper_yaml).cadence
    assert cfg.exit_interval_seconds < 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement la CLI, `pyproject.toml` y el README**

`build_engine` es el único lugar que construye dependencias concretas (`CexDataSource`, `PaperExecutor`, `TelegramNotifier` o `ConsoleNotifier`), de modo que la inversión de dependencias se mantenga.

`run` con `mode: live` termina con código distinto de cero y un mensaje claro de que la ejecución real llega en el plan 3. Es preferible negarse explícitamente a arrancar que ejecutar en simulado cuando el usuario pidió real.

`ConfigError` se captura en la frontera de la CLI y se muestra como mensaje, no como traza.

**Protecciones omitidas: romper el silencio.** Las protecciones valen `0` por defecto, y `0` las desactiva. Eso significa que una configuración que simplemente **no escriba** la sección `protections` corre sin ninguna de las cuatro y sin ninguna señal. El `extra="forbid"` atrapa una errata *dentro* de la sección, pero no una sección ausente. Por tanto, en el arranque:

- En `mode: live`, la clave `protections` **debe estar presente**. Así, correr sin protecciones es una decisión explícita y escrita, no un olvido. Si falta, `ConfigError`.
- En `paper` y en `backtest`, si las cuatro están a cero se emite un **aviso** al arrancar, por el notificador y por la salida estándar, diciendo cuáles están desactivadas.

Tests: `mode: live` sin sección `protections` lanza `ConfigError` nombrando la clave; `mode: paper` con todo a cero arranca pero avisa; una configuración con protecciones activas no avisa.

`run` monta dos trabajos independientes con **`apscheduler` (3.11.3, ya instalado)**: `CronTrigger` para la entrada con `cadence.entry_cron`, e `IntervalTrigger` para la salida con `cadence.exit_interval_seconds`. `--once` ejecuta un ciclo de cada uno y termina, sin planificador.

Razón para no escribirlo a mano: un bucle propio hay que enseñarle qué hacer cuando un ciclo tarda más que su intervalo, cuando el equipo se suspende y se pierden disparos, y cuando hay que reanudar tras un reinicio. `apscheduler` ya resuelve los tres. Configúralo con `max_instances=1` y `coalesce=True`: si el equipo despierta tras dos horas suspendido, se ejecuta **un** ciclo de entrada, no dos encolados, y nunca dos ciclos a la vez sobre la misma base de datos.

El planificador se inyecta para que los tests no esperen en tiempo real.

`pyproject.toml` fija `requires-python = ">=3.11"` y las dependencias: `pydantic`, `pyyaml`, `ccxt`, `typer`, `httpx`, y `pytest` en el grupo de desarrollo. El README documenta instalación, el fichero de configuración y los comandos.

- [ ] **Step 4: Run the full suite**

Run: `pytest -v`
Expected: PASS, toda la batería

- [ ] **Step 5: Commit**

```bash
git add cli.py pyproject.toml README.md tests/test_cli.py
git commit -m "feat(cli): añadir CLI, empaquetado y documentación"
```

---

### Task 18: Protecciones

**Se ejecuta después de la Tarea 11 y ANTES de la Tarea 12**, porque el motor las consume. El número es alto solo porque se añadió al plan más tarde.

**Files:**
- Create: `core/rules/protections.py`
- Modify: `core/config.py` (sección `protections` y `groups`). **No toca `core/rules/risk.py`**: las protecciones las invoca el motor (Tarea 12), no los gates de riesgo. Son dos decisiones independientes que el motor consulta en orden.
- Test: `tests/core/rules/test_protections.py`

**Interfaces:**
- Consumes: `Position`, `Fill`, `ExitReason`, `RejectReason` (T1), `Portfolio` (T11).
- Produces: `ProtectionsConfig` con `cooldown_minutes: int`, `stop_loss_streak: int`, `streak_window_hours: int`, `streak_pause_hours: int`, `max_drawdown_pct: Decimal`, `max_usd_per_group: Decimal`, y `groups: dict[str, list[str]]` en `BotConfig`. `evaluate_protections(symbol, amount_usd, state, cfg, now) -> ProtectionVerdict`. `ProtectionVerdict` congelada con `blocked: bool`, `reason: RejectReason | None`, `until: datetime | None`. Nuevos miembros de `RejectReason`: `COOLDOWN`, `STOP_LOSS_STREAK`, `MAX_DRAWDOWN`, `MAX_USD_PER_GROUP`. `group_of(symbol, groups) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_cooldown_blocks_reentry_after_a_loss():
    # cerró en pérdida hace 30 min, cooldown 60 -> bloqueado
    v = evaluate_protections("BONK", D50, state_with_loss("BONK", minutes_ago=30), cfg(cooldown=60), NOW)
    assert v.blocked and v.reason is RejectReason.COOLDOWN

def test_cooldown_expires():
    v = evaluate_protections("BONK", D50, state_with_loss("BONK", minutes_ago=61), cfg(cooldown=60), NOW)
    assert v.blocked is False

def test_cooldown_does_not_apply_to_a_profitable_close():
    # solo las pérdidas enfrían; cerrar en ganancia no castiga
    v = evaluate_protections("BONK", D50, state_with_win("BONK", minutes_ago=5), cfg(cooldown=60), NOW)
    assert v.blocked is False

def test_cooldown_is_per_symbol():
    v = evaluate_protections("WIF", D50, state_with_loss("BONK", minutes_ago=5), cfg(cooldown=60), NOW)
    assert v.blocked is False

def test_stop_loss_streak_pauses_all_entries():
    # 3 stop-loss en 24h -> bloquea cualquier símbolo, no solo los de la racha
    v = evaluate_protections("ETH/USDT", D50, state_with_stops(3, hours=20), cfg(streak=3, window=24), NOW)
    assert v.blocked and v.reason is RejectReason.STOP_LOSS_STREAK

def test_stop_loss_streak_ignores_stops_outside_the_window():
    v = evaluate_protections("ETH/USDT", D50, state_with_stops(3, hours=30), cfg(streak=3, window=24), NOW)
    assert v.blocked is False

def test_only_stop_loss_closes_count_towards_the_streak():
    # tres cierres por time-stop no son una racha de stops
    v = evaluate_protections("ETH/USDT", D50, state_with_closes(ExitReason.TIME_STOP, 3), cfg(streak=3), NOW)
    assert v.blocked is False

def test_max_drawdown_blocks_entries():
    v = evaluate_protections("ETH/USDT", D50, state(peak=D1000, equity=D880), cfg(max_dd=10), NOW)
    assert v.blocked and v.reason is RejectReason.MAX_DRAWDOWN

def test_max_drawdown_allows_exactly_at_threshold():
    # 900 desde un pico de 1000 es exactamente -10%: desigualdad estricta
    assert evaluate_protections("ETH/USDT", D50, state(peak=D1000, equity=D900), cfg(max_dd=10), NOW).blocked is False

def test_correlated_group_exposure_is_summed():
    # BONK y WIF son el mismo grupo: 60+60 = 120 > 100
    st = state(exposure={"BONK": D60}, groups={"solana_memes": ["BONK", "WIF"]})
    v = evaluate_protections("WIF", D60, st, cfg(max_group=100), NOW)
    assert v.blocked and v.reason is RejectReason.MAX_USD_PER_GROUP

def test_symbols_in_different_groups_do_not_add_up():
    st = state(exposure={"BONK": D60}, groups={"solana_memes": ["BONK"], "majors": ["ETH/USDT"]})
    assert evaluate_protections("ETH/USDT", D60, st, cfg(max_group=100), NOW).blocked is False

def test_ungrouped_symbol_is_its_own_group():
    assert group_of("ETH/USDT", {"solana_memes": ["BONK"]}) == "ETH/USDT"

def test_protections_never_block_exits():
    # las protecciones solo se consultan en el camino de entrada
    assert "exit" not in inspect.signature(evaluate_protections).parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/rules/test_protections.py -v`
Expected: FAIL con `ModuleNotFoundError`

- [ ] **Step 3: Implement `evaluate_protections` y `group_of` en `core/rules/protections.py`**

Función pura. Evalúa en este orden y devuelve en la primera coincidencia: racha de stop-loss (la más global), drawdown máximo, cooldown del símbolo, exposición del grupo. Todos los umbrales con desigualdad estricta, como el resto del proyecto.

`group_of` devuelve el nombre del grupo que contiene el símbolo, o el propio símbolo si no está en ninguno. Así el límite por grupo nunca es más laxo que el límite por token.

Las protecciones **solo bloquean entradas**. Una posición abierta se sigue vigilando y cerrando con normalidad pase lo que pase: si el drawdown dispara y además una posición toca su stop, la posición se cierra. Bloquear salidas convertiría una protección en una trampa.

El motor (Tarea 12) las consulta **después** del apagado por pérdida diaria y **antes** de los gates de §4.5, porque su motivo es más informativo.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/rules/test_protections.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/rules/protections.py core/config.py core/rules/risk.py tests/core/rules/test_protections.py
git commit -m "feat(core): añadir protecciones de frecuencia y exposición por grupo"
```

---

## Qué tienes al terminar

Motor de decisión completo y probado, backtesting determinista sobre el mismo motor, ejecución simulada con precios reales de exchange, notificaciones por Telegram y una CLI para operar y backtestear. Cubre las fases 1–5 del spec §11.

**Siguiente:** plan 2 (capa conversacional, fases 6–8). La ejecución real llega en el plan 3, deliberadamente después de que el control por chat esté probado.
