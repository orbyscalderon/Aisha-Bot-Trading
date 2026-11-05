"""Motor de decisión: une señales, riesgo, tamaño, ejecución y salidas.

Recibe TODAS sus dependencias por constructor y no construye ninguna: es lo
que permite que el backtest use este mismo motor sin cambios (y por eso el
backtest es fiel).

Orden de ``run_entry_cycle`` (cada paso auditado):

1. Kill switch.
2. ``utc_day`` fijado UNA vez para todo el ciclo.
3. Allowlist congelada (``ensure_frozen_allowlist``, nunca ``freeze_allowlist``).
4. Cursor y señales.
5. Filtro de allowlist.
6. Por señal: cotización válida; tamaño y techo duro (el techo se audita si
   recorta); estado de riesgo desde ``Portfolio``; apagado por pérdida diaria;
   protecciones; gates de riesgo; guardia de cantidad cero; reserva del intent
   (DESPUÉS de todos los rechazos: un ``client_order_id`` reservado es
   irrepetible); envío; posición y fill.
7. Cursor con ``clamp_cursor``, solo al final y solo sin excepción.

``run_exit_cycle`` no mira el kill switch: las salidas siguen siempre para no
dejar posiciones huérfanas. Las protecciones nunca bloquean salidas.

Decisiones que el plan no fijaba:

- Sin cursor previo, la primera consulta pide señales desde ``now``: nunca se
  copia histórico al arrancar.
- Con el circuit breaker abierto se detienen las ENTRADAS (no las salidas) y
  el cursor no avanza, para no perder las señales sin procesar.
- El pico de capital (protección de drawdown) se persiste en ``bot_state``.
- Si el apagado por pérdida diaria corta el ciclo, las señales pendientes se
  descartan (una señal copiada horas tarde es una apuesta, no copy-trading),
  pero se audita y se avisa cuántas fueron.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from core.config import BotConfig
from core.idempotency import client_order_id
from core.models import (
    Order, Position, Quote, RejectReason, Side, Signal,
)
from core.portfolio import Portfolio
from core.rules.exits import evaluate_exit, next_peak_price
from core.rules.protections import ProtectionState, evaluate_protections
from core.rules.risk import RiskAction, build_risk_state, evaluate_risk
from core.rules.sizing import SizingContext, apply_hard_cap, calculate_size
from core.safety import CircuitBreaker, is_kill_switch_active, mode_prefix
from core.signals import clamp_cursor, ensure_frozen_allowlist, filter_by_allowlist
from ports.market import MarketDataSource, OrderExecutor, QuoteUnavailable, ZeroQuantity
from ports.notifier import Level, Notifier
from ports.signal_source import SignalSource
from storage.audit import AuditLog
from storage.repository import Repository


_PEAK_KEY = "peak_equity_usd"


@dataclass(frozen=True, slots=True)
class EntryCycleResult:
    opened: list[Position] = field(default_factory=list)
    rejected: list[tuple[Signal, RejectReason]] = field(default_factory=list)
    cursor_advanced: bool = False
    shutdown: bool = False


@dataclass(frozen=True, slots=True)
class ExitCycleResult:
    closed: list[Position] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        config: BotConfig,
        repo: Repository,
        audit: AuditLog,
        portfolio: Portfolio,
        data_source: MarketDataSource,
        executor: OrderExecutor,
        notifier: Notifier,
        circuit_breaker: CircuitBreaker,
        project_root: Path,
        session_id: str = "default",
    ) -> None:
        self._cfg = config
        self._repo = repo
        self._audit = audit
        self._portfolio = portfolio
        self._data = data_source
        self._executor = executor
        self._notifier = notifier
        self._breaker = circuit_breaker
        self._root = project_root
        self._session_id = session_id

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    # --- utilidades ---

    def _notify(self, message: str, level: Level) -> None:
        self._notifier.send(f"{mode_prefix(self._cfg.mode)} | {message}", level)

    def _log(self, event: str, now: datetime, **payload: object) -> None:
        self._audit.record(event, payload, now)

    def _quote(self, symbol: str) -> Quote | None:
        """Cotización válida (precio > 0) o None."""
        try:
            quote = self._data.get_quote(symbol)
        except QuoteUnavailable:
            return None
        if quote is None or quote.price <= 0:
            return None
        return quote

    def _reject(
        self, result: EntryCycleResult, signal: Signal, reason: RejectReason, now: datetime,
        **extra: object,
    ) -> None:
        result.rejected.append((signal, reason))
        self._log("signal_rejected", now, signal_id=signal.id, symbol=signal.symbol,
                  reason=reason.value, **extra)

    # --- entradas ---

    def run_entry_cycle(self, source: SignalSource, now: datetime) -> EntryCycleResult:
        result = EntryCycleResult()
        if is_kill_switch_active(self._root):
            self._log("kill_switch_active", now, source=source.name)
            self._notify("kill switch activo: entradas bloqueadas", Level.WARNING)
            return result
        if self._breaker.is_open:
            self._log("circuit_breaker_open", now, source=source.name)
            self._notify("circuit breaker abierto: entradas bloqueadas", Level.CRITICAL)
            return result

        utc_day = now.date()  # una sola vez: un cambio de día a mitad no mueve la contabilidad
        try:
            halted, shutdown, last_seen = self._entry_body(source, now, utc_day, result)
            if shutdown:
                result = _with(result, shutdown=True)
            if not halted:
                result = self._advance_cursor(source, now, result, last_seen)
        except Exception as exc:  # noqa: BLE001 - el ciclo nunca debe tumbar al proceso
            self._log("cycle_error", now, source=source.name, error=repr(exc))
            self._notify(f"error en el ciclo de entrada: {exc!r}", Level.CRITICAL)
            return _with(result, cursor_advanced=False)
        return result

    def _advance_cursor(
        self, source: SignalSource, now: datetime, result: EntryCycleResult,
        last_seen: datetime | None,
    ) -> EntryCycleResult:
        candidate = last_seen if last_seen is not None else now
        self._repo.advance_cursor(source.name, clamp_cursor(candidate, now))
        self._log("cursor_advanced", now, source=source.name)
        return _with(result, cursor_advanced=True)

    def _entry_body(
        self, source: SignalSource, now: datetime, utc_day, result: EntryCycleResult
    ) -> tuple[bool, bool, datetime | None]:
        """Procesa las señales. Devuelve (detenido, apagado, última marca de tiempo vista)."""
        following = self._cfg.allowlist or source.list_followed()
        allowlist = ensure_frozen_allowlist(self._session_id, following, self._repo)

        # Sin cursor previo se pide desde ``now``: es lo que evita copiar al arrancar
        # todo el histórico de un trader (operaciones viejas, a precio de hoy).
        since = self._repo.get_cursor(source.name) or now
        signals = source.fetch_trades(since)
        last_seen = max((s.timestamp for s in signals), default=None)

        accepted, not_allowed = filter_by_allowlist(signals, allowlist)
        for signal, reason in not_allowed:
            self._reject(result, signal, reason, now, trader_id=signal.trader_id)

        for index, signal in enumerate(accepted):
            if signal.side is not Side.BUY:
                self._log("signal_ignored", now, signal_id=signal.id, side=signal.side.value)
                continue
            if self._breaker.is_open:
                self._log("circuit_breaker_open", now, source=source.name)
                self._notify("circuit breaker abierto: ciclo detenido", Level.CRITICAL)
                return True, False, last_seen
            if self._process_buy(signal, now, utc_day, result):
                # Apagado por pérdida diaria: fin del ciclo. El cursor avanza (no se
                # ejecutan señales rancias), pero lo descartado se audita y se avisa.
                discarded = len(accepted) - index - 1
                self._log("signals_discarded_by_shutdown", now, discarded=discarded,
                          source=source.name)
                self._notify(
                    f"apagado por pérdida diaria: {discarded} señales sin procesar descartadas",
                    Level.WARNING,
                )
                return False, True, last_seen
        return False, False, last_seen

    def _process_buy(
        self, signal: Signal, now: datetime, utc_day, result: EntryCycleResult
    ) -> bool:
        """Devuelve True si hay que terminar el ciclo (SHUTDOWN)."""
        cfg = self._cfg
        quote = self._quote(signal.symbol)
        if quote is None:
            self._reject(result, signal, RejectReason.INVALID_QUOTE, now)
            return False

        exec_cfg = cfg.execution[signal.market]
        equity = self._data.get_balance()
        peak_equity = self._update_peak_equity(equity)
        sized = calculate_size(
            signal, cfg.sizing,
            SizingContext(equity, cfg.exits.stop_loss_pct, exec_cfg.slippage_bps, exec_cfg.fee_bps),
        )
        amount = apply_hard_cap(sized.amount_usd, cfg.risk.hard_max_order_usd)
        if amount != sized.amount_usd:
            # Un techo que recorta es que el tamaño salió absurdo: eso es un bug a ver.
            self._log("hard_cap_applied", now, signal_id=signal.id, symbol=signal.symbol,
                      reason=RejectReason.HARD_CAP.value,
                      before=sized.amount_usd, after=amount)
            self._notify(
                f"techo duro recortó {signal.symbol}: {sized.amount_usd} -> {amount} USD",
                Level.WARNING,
            )

        exposure = self._portfolio.exposure_by_symbol()
        state = build_risk_state(
            utc_day, exposure, self._portfolio.open_position_count(),
            self._portfolio.spent_usd(utc_day), self._portfolio.realized_pnl_usd(utc_day),
        )
        verdict = evaluate_risk(amount, signal.symbol, state, cfg.risk)

        # 1. Apagado por pérdida diaria: antes que cualquier protección o rechazo.
        if verdict.action is RiskAction.SHUTDOWN:
            self._log("shutdown", now, signal_id=signal.id,
                      realized_pnl_usd=state.realized_pnl_usd_today)
            self._notify("apagado por pérdida diaria máxima", Level.CRITICAL)
            result.rejected.append((signal, RejectReason.MAX_DRAWDOWN))
            return True

        # 2. Protecciones: su motivo es más informativo que el de los gates.
        protection = evaluate_protections(
            signal.symbol, amount, self._protection_state(exposure, equity, peak_equity, now),
            cfg.protections, now,
        )
        if protection.blocked:
            assert protection.reason is not None
            self._reject(result, signal, protection.reason, now,
                         until=protection.until, source="protection")
            return False

        # 3. Gates de riesgo.
        if verdict.action is RiskAction.REJECT:
            assert verdict.reason is not None
            self._reject(result, signal, verdict.reason, now, source="risk")
            return False

        # 4. Guardia de cantidad cero. NO es la cantidad definitiva: esa la produce
        # el ejecutor desde el precio deslizado y Position.quantity sale del Fill.
        guard_qty = (amount / quote.price).quantize(
            Decimal(1).scaleb(-exec_cfg.quantity_precision), rounding=ROUND_DOWN
        )
        if amount <= 0 or guard_qty <= 0:
            self._reject(result, signal, RejectReason.ZERO_QUANTITY, now, amount_usd=amount)
            return False

        self._log("signal_accepted", now, signal_id=signal.id, symbol=signal.symbol,
                  amount_usd=amount, used_fallback=sized.used_fallback)

        # 5. Reserva del intent: después de todos los rechazos.
        coid = client_order_id(signal.id, signal.trader_id, signal.symbol, Side.BUY, now)
        reserved = self._repo.reserve_intent(
            coid, {"signal_id": signal.id, "symbol": signal.symbol, "amount_usd": str(amount)}, now
        )
        if not reserved:
            self._reject(result, signal, RejectReason.DUPLICATE, now, client_order_id=coid)
            return False
        self._log("order_reserved", now, signal_id=signal.id, client_order_id=coid)

        order = Order(coid, signal.market, signal.symbol, Side.BUY, amount, quote.price)
        try:
            self._repo.mark_intent_sent(coid)
            fill = self._executor.submit_buy(order)
        except (ZeroQuantity, Exception) as exc:  # noqa: BLE001
            self._repo.mark_intent_failed(coid, repr(exc))
            self._breaker.record_failure()
            self._log("order_failed", now, client_order_id=coid, error=repr(exc))
            self._notify(f"orden fallida {signal.symbol}: {exc!r}", Level.WARNING)
            return False

        self._breaker.record_success()
        position = Position(
            id=f"pos-{coid}", signal_id=signal.id, trader_id=signal.trader_id,
            market=signal.market, symbol=signal.symbol, entry_price=fill.price,
            quantity=fill.quantity, amount_usd=amount, opened_at=now, peak_price=fill.price,
        )
        self._repo.save_position(position)
        self._repo.save_fill(fill)
        self._repo.mark_intent_confirmed(coid, fill)
        self._log("position_opened", now, position_id=position.id, client_order_id=coid,
                  price=fill.price, quantity=fill.quantity, amount_usd=amount)
        result.opened.append(position)
        return False

    def _update_peak_equity(self, equity: Decimal) -> Decimal:
        """Pico de capital persistido: si se perdiera al reiniciar, el drawdown se mediría
        desde el suelo y la protección se apagaría justo tras una caída."""
        stored = self._repo.get_state(_PEAK_KEY)
        peak = Decimal(stored) if stored is not None else equity
        if stored is None or equity > peak:
            peak = max(peak, equity)
            self._repo.set_state(_PEAK_KEY, str(peak))
        return peak

    def _protection_state(
        self, exposure: dict[str, Decimal], equity: Decimal, peak_equity: Decimal,
        now: datetime,
    ) -> ProtectionState:
        p = self._cfg.protections
        lookback = max(timedelta(hours=p.streak_window_hours), timedelta(minutes=p.cooldown_minutes))
        return ProtectionState(
            closed_positions=self._portfolio.closed_positions_since(now - lookback),
            peak_equity_usd=peak_equity,
            equity_usd=equity,
            exposure_by_symbol=exposure,
            groups=self._cfg.groups,
        )

    # --- salidas ---

    def run_exit_cycle(self, now: datetime) -> ExitCycleResult:
        closed: list[Position] = []
        errors: list[str] = []
        for position in self._portfolio.open_positions():
            try:
                quote = self._quote(position.symbol)
                if quote is None:
                    raise QuoteUnavailable(position.symbol)
            except QuoteUnavailable:
                msg = f"{position.id}: sin cotización válida para {position.symbol}"
                errors.append(msg)
                self._log("exit_error", now, position_id=position.id, error=msg)
                continue
            try:
                done = self._process_exit(position, quote, now)
            except Exception as exc:  # noqa: BLE001 - una posición no aborta a las demás
                msg = f"{position.id}: {exc!r}"
                errors.append(msg)
                self._log("exit_error", now, position_id=position.id, error=msg)
                self._notify(f"error cerrando {position.symbol}: {exc!r}", Level.WARNING)
                continue
            if done is not None:
                closed.append(done)
        return ExitCycleResult(closed, errors)

    def _process_exit(self, position: Position, quote: Quote, now: datetime) -> Position | None:
        # La monotonía del pico vive en next_peak_price; el repositorio guarda lo que le den.
        peak = next_peak_price(position, quote)
        if peak != position.peak_price:
            self._repo.update_peak_price(position.id, peak)
            position = replace(position, peak_price=peak)

        decision = evaluate_exit(position, quote, now, self._cfg.exits, None)
        if decision is None:
            return None

        coid = client_order_id(position.signal_id, position.trader_id, position.symbol,
                               Side.SELL, now)
        if not self._repo.reserve_intent(
            coid, {"position_id": position.id, "reason": decision.reason.value}, now
        ):
            self._log("exit_duplicate", now, position_id=position.id, client_order_id=coid)
            return None
        self._log("exit_reserved", now, position_id=position.id, client_order_id=coid,
                  reason=decision.reason.value)
        order = Order(coid, position.market, position.symbol, Side.SELL, position.amount_usd,
                      quote.price, position_id=position.id, quantity=position.quantity)
        try:
            self._repo.mark_intent_sent(coid)
            fill = self._executor.submit_sell(order)
        except Exception as exc:  # noqa: BLE001
            self._repo.mark_intent_failed(coid, repr(exc))
            self._log("order_failed", now, client_order_id=coid, error=repr(exc))
            raise
        if fill.position_id is None:
            fill = replace(fill, position_id=position.id)
        self._repo.save_fill(fill)
        self._repo.close_position(position.id, fill.price, decision.reason, now)
        self._repo.mark_intent_confirmed(coid, fill)
        self._log("position_closed", now, position_id=position.id, reason=decision.reason.value,
                  price=fill.price)
        self._notify(f"cerrada {position.symbol}: {decision.reason.value}", Level.INFO)
        return self._repo.get_position(position.id)


def _with(result: EntryCycleResult, **changes: object) -> EntryCycleResult:
    return replace(result, **changes)
