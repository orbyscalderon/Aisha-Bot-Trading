"""CLI del bot: ``run``, ``backtest``, ``status``, ``positions`` y ``stop``.

Reglas de este módulo:

- ``build_engine`` es el ÚNICO sitio que construye dependencias concretas del motor
  (fuente de datos, ejecutor, notificador, base de datos). Todo lo demás las recibe:
  es lo que permite que el backtest use el mismo ``Engine`` que opera.
- ``run`` con ``mode: live`` se niega y termina con código distinto de cero. Negarse
  es mejor que ejecutar en simulado cuando se pidió real.
- ``ConfigError`` (y los errores de ficheros de entrada) se muestran como mensaje,
  nunca como traza: una errata en un YAML no debe filtrar rutas internas.
- Los ciclos los lanza apscheduler con ``max_instances=1`` y ``coalesce=True``: un
  equipo suspendido dos horas ejecuta UN ciclo de entrada al despertar, no dos. Entrada
  y salida comparten un cerrojo, así que nunca hay dos ciclos a la vez sobre la misma
  base de datos. El planificador se inyecta para que los tests no esperen tiempo real.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import typer
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from adapters.cex.source import CexCsvSignalSource, CexDataSource
from adapters.notify.console import ConsoleNotifier
from adapters.notify.telegram import TelegramNotifier
from adapters.paper.executor import PaperExecutor
from backtest.runner import run_backtest
from core.config import BotConfig, ConfigError, ProtectionsConfig, load_config
from core.engine import Engine
from core.models import Market, Mode, Quote
from core.portfolio import Portfolio
from core.safety import KILL_SWITCH_FILENAME, CircuitBreaker, is_kill_switch_active, mode_prefix
from ports.market import MarketDataSource
from ports.notifier import Level, Notifier
from storage.audit import AuditLog
from storage.db import init_db
from storage.repository import Repository

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/config.yaml")
DEFAULT_CAPITAL = Decimal(10000)
DEFAULT_EXCHANGE = "binance"
_PROTECTION_FIELDS = ("cooldown_minutes", "stop_loss_streak", "max_drawdown_pct", "max_usd_per_group")

app = typer.Typer(
    help="Bot de copy-trading: backtest y ejecución simulada. La ejecución real no existe todavía.",
    no_args_is_help=True,
    add_completion=False,
)


class InvalidExchange(Exception):
    """El exchange pedido no existe en ccxt (única causa esperada de fallo al construir la fuente)."""


class LiveModeNotImplemented(RuntimeError):
    """La ejecución real llega en el plan 3."""


@dataclass
class CliDeps:
    """Dependencias inyectables en ``ctx.obj``; ``None`` significa "construir la real"."""

    scheduler: Any = None
    data_source: MarketDataSource | None = None
    notifier: Notifier | None = None


# --- composición ---

class PaperCapitalSource(MarketDataSource):
    """Cotizaciones reales, capital simulado.

    En simulado el capital no puede salir de la cuenta real del exchange (ni existir):
    es el capital inicial más el efectivo de los fills guardados más el valor a mercado
    de lo abierto. Mismo criterio que usa el backtest.
    """

    def __init__(self, inner: MarketDataSource, repo: Repository, capital: Decimal) -> None:
        self._inner = inner
        self._repo = repo
        self._capital = capital

    def get_quote(self, symbol: str) -> Quote:
        return self._inner.get_quote(symbol)

    def get_balance(self) -> Decimal:
        return Portfolio(self._repo).total_equity_usd(self._inner.get_quote, self._capital)


def _make_notifier(mode: Mode) -> Notifier:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id, mode)
    return ConsoleNotifier(mode)


def _build(
    config: BotConfig,
    root: Path,
    *,
    exchange_id: str = DEFAULT_EXCHANGE,
    data_source: MarketDataSource | None = None,
    notifier: Notifier | None = None,
    paper_capital: Decimal = DEFAULT_CAPITAL,
) -> tuple[Engine, Notifier, AuditLog]:
    if config.mode is Mode.LIVE:
        raise LiveModeNotImplemented(
            "modo live no implementado: la ejecución real llega en el plan 3"
        )
    # La fuente de datos va ANTES de tocar el disco: un exchange inválido no deja una base
    # de datos creada ni una conexión colgando. Solo se captura el error esperado.
    if data_source is not None:
        raw_data = data_source
    else:
        try:
            raw_data = CexDataSource(exchange_id, ignore_assets=config.cex.ignore_assets)
        except ValueError as exc:
            raise InvalidExchange(f"exchange no válido {exchange_id!r}: {exc}") from exc
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # Una base por modo: las posiciones simuladas nunca se mezclan con las reales.
    # check_same_thread=False porque los ciclos corren en hilos del planificador; el
    # acceso del motor está serializado por el cerrojo de ``build_jobs``.
    db_path = root / f"{config.mode.value}.db"
    conn = init_db(db_path, check_same_thread=False)
    repo = Repository(conn)
    data = PaperCapitalSource(raw_data, repo, paper_capital)
    exec_cfg = config.execution[Market.CEX]
    executor = PaperExecutor(
        data, exec_cfg.slippage_bps, exec_cfg.fee_bps, exec_cfg.quantity_precision
    )
    notifier = notifier if notifier is not None else _make_notifier(config.mode)
    engine = Engine(
        config=config, repo=repo, audit=AuditLog(conn), portfolio=Portfolio(repo),
        data_source=data, executor=executor, notifier=notifier,
        circuit_breaker=CircuitBreaker(3), project_root=root,
    )
    # Auditoría para la alerta de cerrojo vencido: conexión PROPIA, porque la del motor puede
    # estar a mitad de una transacción en el hilo de entrada colgado, y escribir en ella
    # sin el cerrojo podría mezclarse con esa transacción.
    side_audit = AuditLog(init_db(db_path, check_same_thread=False))
    return engine, notifier, side_audit


def build_engine(config: BotConfig, root: Path, **kwargs: Any) -> Engine:
    """Compone el motor con sus dependencias concretas. Único sitio que lo hace."""
    engine, _notifier, side_audit = _build(config, root, **kwargs)
    # Solo ``run`` cablea la auditoría del cerrojo en ``build_jobs``; aquí se descartaría
    # con su conexión abierta.
    side_audit.close()
    return engine


def default_scheduler() -> BlockingScheduler:
    return BlockingScheduler(timezone=timezone.utc)


def exit_lock_timeout(exit_interval_seconds: float) -> float:
    """Cuánto espera el ciclo de salida a que la entrada suelte el cerrojo.

    Mayor que el intervalo de salida, para no avisar por solapes normales, y acotado
    para que un ciclo de entrada colgado no silencie las salidas indefinidamente.
    """
    return exit_interval_seconds * 2 + 5


def _summarise_exit_errors(result: Any, notifier: Notifier | None) -> None:
    """Un WARNING por ciclo con los errores que el motor NO notificó ya individualmente.

    El motor avisa solo (CRITICAL/WARNING) de las salidas rechazadas o falladas; resumirlas
    otra vez sería duplicar. Aquí solo quedan las silenciosas (p. ej. sin cotización). Sin
    nada que decir no se envía nada: una alerta que suena cada minuto acaba silenciada.
    """
    errors = list(getattr(result, "errors", None) or ())
    already = list(getattr(result, "alerted", None) or ())
    pending = []
    for e in errors:
        if e in already:
            already.remove(e)  # multiconjunto: dos errores iguales no se compensan entre sí
        else:
            pending.append(e)
    if not pending or notifier is None:
        return
    shown = "; ".join(pending[:3]) + ("; ..." if len(pending) > 3 else "")
    notifier.send(f"ciclo de salida con {len(pending)} error(es): {shown}", Level.WARNING)


def build_jobs(
    engine: Engine,
    source: Any,
    *,
    notifier: Notifier | None = None,
    audit: Any = None,
    lock_timeout: float | None = None,
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Funciones de entrada y salida que comparten un cerrojo.

    ``max_instances=1`` impide solapar un job consigo mismo; el cerrojo impide que la
    entrada y la salida toquen la misma base de datos a la vez.

    El ciclo de salida adquiere el cerrojo **con tiempo límite**. Sin él, un ciclo de
    entrada colgado —una llamada al exchange que no vuelve— se quedaría con el cerrojo,
    la salida esperaría para siempre y ``max_instances=1`` haría que las siguientes se
    saltaran: los stop-loss dejarían de dispararse con posiciones abiertas, sin que
    nada fallara a la vista. Al expirar NO se ejecuta sin cerrojo (dos ciclos sobre la
    misma base es otro problema): se avisa con nivel CRITICAL y se audita.
    """
    lock = threading.Lock()
    timeout = exit_lock_timeout(60) if lock_timeout is None else lock_timeout

    def entry() -> None:
        with lock:
            result = engine.run_entry_cycle(source, datetime.now(timezone.utc))
        # Un rechazo es operación normal (una puerta de riesgo haciendo su trabajo): no se
        # avisa, solo queda en el log para quien active DEBUG.
        for item in getattr(result, "rejected", None) or ():
            logger.debug("entrada rechazada: %s", item)

    def exit_() -> None:
        if not lock.acquire(timeout=timeout):
            message = (
                "el ciclo de salida no pudo ejecutarse: el de entrada lleva más de "
                f"{timeout:g} s ocupando el cerrojo. Las posiciones abiertas están "
                "sin vigilar hasta que se libere."
            )
            # Primero la alerta: el notificador se traga sus propios errores, así que sale
            # pase lo que pase. La auditoría es una escritura real y puede fallar.
            if notifier is not None:
                notifier.send(message, Level.CRITICAL)
            if audit is not None:
                try:
                    audit.record(
                        "exit_cycle_skipped_lock_timeout",
                        {"timeout_seconds": str(timeout)},
                        datetime.now(timezone.utc),
                    )
                except Exception as exc:  # noqa: BLE001 - no puede tapar la alerta ya enviada
                    logger.error("no se pudo auditar el ciclo de salida omitido: %s", type(exc).__name__)
            return
        try:
            result = engine.run_exit_cycle(datetime.now(timezone.utc))
        finally:
            lock.release()
        _summarise_exit_errors(result, notifier)

    return entry, exit_


# --- utilidades de la frontera ---

def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


def _load(config_path: Path) -> BotConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise _fail(f"error de configuración: {exc}", 2) from None


def _deps(ctx: typer.Context) -> CliDeps:
    return ctx.obj if isinstance(ctx.obj, CliDeps) else CliDeps()


def _say(config: BotConfig, message: str) -> None:
    typer.echo(f"{mode_prefix(config.mode)} | {message}")


def _db_path(config: BotConfig, root: Path) -> Path:
    return Path(root) / f"{config.mode.value}.db"


def _protections_off(protections: ProtectionsConfig) -> bool:
    return not any(getattr(protections, name) for name in _PROTECTION_FIELDS)


def warn_if_no_protections(config: BotConfig, notifier: Notifier) -> None:
    """Una sección ``protections`` ausente corre sin ninguna y sin señal: se rompe el silencio."""
    if not _protections_off(config.protections):
        return
    message = (
        "ATENCIÓN: sin protecciones activas (" + ", ".join(f"{n}=0" for n in _PROTECTION_FIELDS)
        + "). Nada frena a este bot si insiste en un token que cae. "
        "Si es deliberado, ignora este aviso; si no, añade la sección `protections`."
    )
    _say(config, message)
    notifier.send(message, Level.WARNING)


def warn_if_copy_sell_primary(config: BotConfig, notifier: Notifier) -> None:
    """``primary: copy_sell`` no hace nada en el plan 1: que no parezca que sí."""
    if config.exits.primary != "copy_sell":
        return
    message = (
        "ATENCIÓN: exits.primary es copy_sell, pero copiar las ventas del trader NO está "
        "conectado en el plan 1 (llega en un plan posterior): las salidas efectivas son "
        "solo el stop-loss y el time-stop."
    )
    _say(config, message)
    notifier.send(message, Level.WARNING)


# --- lectura de ficheros de entrada del backtest ---

def _parse_ts(text: str) -> datetime:
    ts = datetime.fromisoformat(text.strip())
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _read_signals(path: Path) -> list:
    try:
        return CexCsvSignalSource(path).fetch_trades(None)
    except AttributeError:
        # Fila truncada: el campo ausente llega como None y falla al limpiarlo.
        raise _fail(
            f"error leyendo las señales {path}: hay una fila incompleta o truncada "
            "(se esperan las columnas timestamp,trader_id,symbol,side,amount_usd)"
        ) from None
    except (OSError, KeyError, ValueError, InvalidOperation) as exc:
        raise _fail(f"error leyendo las señales {path}: {type(exc).__name__}: {exc}") from None


def _read_quotes(path: Path) -> list[Quote]:
    try:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            return [
                Quote(row["symbol"].strip(), Decimal(row["price"].strip()), _parse_ts(row["timestamp"]))
                for row in csv.DictReader(fh)
            ]
    except (OSError, KeyError, ValueError, InvalidOperation) as exc:
        raise _fail(
            f"error leyendo las cotizaciones {path} (se esperan las columnas "
            f"timestamp,symbol,price): {type(exc).__name__}: {exc}"
        ) from None


def _parse_capital(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise _fail(f"--capital no es un número: {text!r}", 2) from None
    if not value.is_finite() or value <= 0:
        raise _fail(f"--capital debe ser positivo: {text!r}", 2)
    return value


# --- comandos ---

ConfigOpt = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Fichero YAML de configuración.")
RootOpt = typer.Option(
    Path("."), "--root", help="Carpeta del bot: base de datos y fichero STOP (kill switch)."
)


@app.command()
def run(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    signals: Path | None = typer.Option(
        None, "--signals", help="CSV de señales: timestamp,trader_id,symbol,side,amount_usd."
    ),
    root: Path = RootOpt,
    once: bool = typer.Option(False, "--once", help="Un ciclo de entrada y uno de salida, y termina."),
    exchange: str = typer.Option(DEFAULT_EXCHANGE, "--exchange", help="Exchange (ccxt) de las cotizaciones."),
    capital: str = typer.Option(str(DEFAULT_CAPITAL), "--capital", help="Capital simulado en USD."),
) -> None:
    """Ejecuta el bot en modo simulado (paper)."""
    deps = _deps(ctx)
    cfg = _load(config)
    if cfg.mode is Mode.LIVE:
        # Negarse: ejecutar en simulado creyendo que es real sería la peor confusión posible.
        raise _fail(
            f"{mode_prefix(cfg.mode)} | modo live no implementado: la ejecución real llega en el "
            "plan 3. No se ha ejecutado nada (ni siquiera en simulado). "
            "Usa mode: paper para operar en simulado.",
            3,
        )
    if signals is None:
        raise _fail("falta --signals: indica el CSV de señales a copiar", 2)
    try:
        entry_trigger = CronTrigger.from_crontab(cfg.cadence.entry_cron, timezone=timezone.utc)
    except ValueError as exc:
        raise _fail(f"error de configuración: cadence.entry_cron inválido ({cfg.cadence.entry_cron!r}): {exc}", 2) from None

    # Validar el CSV AL ARRANCAR. `CexCsvSignalSource` lo lee perezosamente dentro del
    # ciclo, así que un fichero ausente o malformado saldría como traza cruda a mitad de
    # ejecución en vez de como mensaje al empezar.
    _read_signals(signals)

    _say(cfg, f"arrancando en simulado (config {cfg.fingerprint()[:12]})")
    try:
        engine, notifier, audit = _build(
            cfg, root, exchange_id=exchange, data_source=deps.data_source,
            notifier=deps.notifier, paper_capital=_parse_capital(capital),
        )
    except InvalidExchange as exc:
        raise _fail(str(exc), 2) from None
    warn_if_no_protections(cfg, notifier)
    warn_if_copy_sell_primary(cfg, notifier)
    source = CexCsvSignalSource(signals)
    entry_job, exit_job = build_jobs(
        engine, source, notifier=notifier, audit=audit,
        lock_timeout=exit_lock_timeout(cfg.cadence.exit_interval_seconds),
    )

    if once:
        now = datetime.now(timezone.utc)
        entry = engine.run_entry_cycle(source, now)
        _say(cfg, f"ciclo de entrada: {len(entry.opened)} abiertas, {len(entry.rejected)} rechazadas")
        closed = engine.run_exit_cycle(now)
        _say(cfg, f"ciclo de salida: {len(closed.closed)} cerradas, {len(closed.errors)} errores")
        return

    scheduler = deps.scheduler if deps.scheduler is not None else default_scheduler()
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": None}
    scheduler.add_job(entry_job, entry_trigger, id="entry", name="ciclo de entrada", **common)
    scheduler.add_job(
        exit_job, IntervalTrigger(seconds=cfg.cadence.exit_interval_seconds, timezone=timezone.utc),
        id="exit", name="ciclo de salida", **common,
    )
    _say(cfg, f"entrada: cron '{cfg.cadence.entry_cron}' (UTC); salida cada {cfg.cadence.exit_interval_seconds} s. Ctrl+C para parar")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        _say(cfg, "detenido")


@app.command()
def backtest(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    signals: Path = typer.Option(..., "--signals", help="CSV de señales: timestamp,trader_id,symbol,side,amount_usd."),
    quotes: Path = typer.Option(..., "--quotes", help="CSV de cotizaciones: timestamp,symbol,price."),
    report: Path | None = typer.Option(None, "--report", help="Escribe el informe HTML de quantstats aquí."),
    step_minutes: int = typer.Option(60, "--step-minutes", min=1, help="Paso del reloj simulado."),
    capital: str = typer.Option(str(DEFAULT_CAPITAL), "--capital", help="Capital inicial simulado en USD."),
) -> None:
    """Reproduce señales pasadas con el mismo motor que opera."""
    deps = _deps(ctx)
    cfg = _load(config)
    sigs = _read_signals(signals)
    qs = _read_quotes(quotes)
    if not sigs:
        raise _fail(f"{signals} no contiene señales")
    if not qs:
        raise _fail(f"{quotes} no contiene cotizaciones")
    start = min(s.timestamp for s in sigs)
    end = max(q.timestamp for q in qs)
    notifier = deps.notifier if deps.notifier is not None else _make_notifier(cfg.mode)
    warn_if_no_protections(cfg, notifier)
    warn_if_copy_sell_primary(cfg, notifier)
    _say(cfg, f"backtest {start.isoformat()} -> {end.isoformat()} (paso {step_minutes} min)")
    try:
        result = run_backtest(cfg, sigs, qs, start, end, timedelta(minutes=step_minutes), _parse_capital(capital))
    except ValueError as exc:
        raise _fail(f"error en el backtest: {exc}") from None

    m = result.metrics
    _say(cfg, f"PnL realizado: {m.total_pnl_usd} USD | drawdown máx.: {m.max_drawdown_usd} USD")
    _say(cfg, f"tasa de acierto: {m.win_rate} | expectativa: {m.expectancy_usd} USD | Sharpe: {m.sharpe}")
    _say(cfg, "cierres por motivo:")
    if m.closes_by_reason:
        for reason, count in sorted(m.closes_by_reason.items(), key=lambda kv: kv[0].name):
            typer.echo(f"  {reason.name}: {count}")
    else:
        typer.echo("  (ninguno)")
    _say(
        cfg,
        f"Posiciones abiertas al final: {result.open_positions_count}   "
        f"(PnL latente: {result.unrealized_pnl_usd:.2f} USD)",
    )
    if report is not None:
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            result.write_report(report, title=f"Backtest {cfg.fingerprint()[:8]}")
        except (OSError, ValueError) as exc:
            raise _fail(f"no se pudo escribir el informe {report}: {exc}") from None
        _say(cfg, f"informe escrito en {report}")


def _open_positions(cfg: BotConfig, root: Path) -> list:
    db = _db_path(cfg, root)
    if not db.exists():
        return []
    conn = init_db(db)
    try:
        return Repository(conn).open_positions()
    finally:
        conn.close()


def _unconfirmed_intents(cfg: BotConfig, root: Path) -> list:
    db = _db_path(cfg, root)
    if not db.exists():
        return []
    conn = init_db(db)
    try:
        return Repository(conn).unconfirmed_intents()
    finally:
        conn.close()


@app.command()
def status(config: Path = ConfigOpt, root: Path = RootOpt) -> None:
    """Modo, kill switch y posiciones abiertas."""
    cfg = _load(config)
    _say(cfg, "estado")
    typer.echo(f"kill switch: {'ACTIVO' if is_kill_switch_active(Path(root)) else 'inactivo'}")
    typer.echo(f"posiciones abiertas: {len(_open_positions(cfg, root))}")
    pending = _unconfirmed_intents(cfg, root)
    typer.echo(f"órdenes sin confirmar: {len(pending)}")
    for intent in pending:
        typer.echo(
            f"  {intent.client_order_id}  {intent.status.value}  "
            f"{intent.payload.get('position_id') or intent.payload.get('symbol', '?')}  "
            f"{intent.created_at.isoformat()}"
        )
    if pending:
        typer.echo(
            "  (una orden en SENT es de resultado incierto: si es una salida, esa posición "
            "no se cerrará sola; comprueba el exchange y reconcilia)"
        )


@app.command()
def positions(config: Path = ConfigOpt, root: Path = RootOpt) -> None:
    """Lista las posiciones abiertas."""
    cfg = _load(config)
    opened = _open_positions(cfg, root)
    _say(cfg, f"posiciones abiertas: {len(opened)}")
    if not opened:
        typer.echo("sin posiciones abiertas")
    for p in opened:
        typer.echo(
            f"  {p.symbol}  cantidad {p.quantity}  entrada {p.entry_price}  "
            f"{p.amount_usd} USD  abierta {p.opened_at.isoformat()}"
        )


@app.command()
def stop(root: Path = RootOpt) -> None:
    """Activa el kill switch: bloquea nuevas entradas (las salidas siguen)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / KILL_SWITCH_FILENAME).write_text("stop\n", encoding="utf-8")
    typer.echo(f"kill switch activado: {root / KILL_SWITCH_FILENAME}. Bórralo para reanudar las entradas.")


if __name__ == "__main__":
    app()
