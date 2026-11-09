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
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from adapters.cex.history import (
    DEFAULT_MAX_REQUESTS,
    DEFAULT_PAGE_LIMIT,
    HistoryError,
    SymbolNotFound,
    fetch_candles,
    make_client as make_history_client,
    validate_timeframe,
    write_quotes_csv,
)
from adapters.cex.source import CexCsvSignalSource, CexDataSource
from adapters.solana.prices import SolanaPriceError, fetch_token_quotes
from adapters.solana.sol_price import SOL_SYMBOLS, SolPriceError, SolPriceTable
from interfaces.chat.telegram_listener import TelegramListener
from interfaces.dispatcher import Dispatcher
from interfaces.nlu.claude import ClaudeParser
from interfaces.nlu.rules import RulesParser
from interfaces.reports import BotReports
from adapters.solana.source import (
    OMISSION_REASONS,
    HeliusError,
    HeliusOrderError,
    fetch_wallet_swaps,
    load_api_key,
    load_token_map,
    make_client as make_solana_client,
    validate_wallet,
    write_signals_csv,
)
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
FETCH_PAGE_LIMIT = DEFAULT_PAGE_LIMIT
FETCH_MAX_REQUESTS = DEFAULT_MAX_REQUESTS
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
    history_client: Any = None  # cliente ccxt de fetch-quotes
    sleep: Callable[[float], None] | None = None  # temporizador de fetch-quotes
    now: Callable[[], datetime] | None = None  # reloj de fetch-quotes y fetch-solana-signals
    solana_client: Any = None  # cliente httpx de fetch-solana-signals
    telegram_client: Any = None  # cliente httpx del control por Telegram
    gecko_client: Any = None  # cliente httpx de fetch-solana-quotes


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


def make_gecko_client() -> Any:
    import httpx
    from adapters.solana.prices import TIMEOUT_SECONDS as _GECKO_TIMEOUT
    return httpx.Client(timeout=_GECKO_TIMEOUT)


def _make_notifier(mode: Mode) -> Notifier:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return TelegramNotifier(token, chat_id, mode)
    return ConsoleNotifier(mode)


def parse_chat_allowlist(raw: str | None) -> frozenset[str]:
    """Lista de chat_id autorizados, separados por comas o puntos y coma.

    Falla cerrado: una cadena vacia da un conjunto vacio, y un conjunto vacio no autoriza a
    nadie. Si significara "todos", un .env a medio rellenar abriria el bot a cualquiera.
    """
    texto = (raw or "").replace(";", ",")
    return frozenset(p.strip() for p in texto.split(",") if p.strip())


def build_chat_listener(
    config: BotConfig,
    root: Path,
    *,
    quote_fn: Any = None,
    client: Any = None,
    llm_client: Any = None,
) -> tuple[TelegramListener | None, str]:
    """Monta el control por Telegram, o explica por que no se puede. Nunca lanza.

    Devuelve ``(escucha, motivo)``. Si ``escucha`` es None, ``motivo`` dice que falta. El bot
    tiene que poder arrancar sin chat: quedarse sin operar porque falta un token de Telegram
    seria dejar de hacer lo importante por no poder hacer lo accesorio.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    autorizados = parse_chat_allowlist(
        os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or os.environ.get("TELEGRAM_CHAT_ID")
    )
    if not token:
        return None, "falta TELEGRAM_BOT_TOKEN"
    if not autorizados:
        return None, "falta TELEGRAM_ALLOWED_CHAT_IDS (o TELEGRAM_CHAT_ID): nadie autorizado"

    # Conexion PROPIA del hilo de chat, por la misma razon que la auditoria del cerrojo: el
    # motor puede estar a mitad de una transaccion en el hilo del planificador.
    conn = init_db(_db_path(config, root), check_same_thread=False)
    reports = BotReports(config.mode, Path(root), lambda: Repository(conn), quote_fn=quote_fn)

    # Las reglas SIEMPRE primero: un comando exacto no debe depender de una llamada de red ni
    # costar dinero. Claude solo entra con lo que las reglas no entienden, y si falla se
    # degrada a UNKNOWN (el usuario recibe la ayuda, el bot sigue operando).
    parsers: list[Any] = [RulesParser()]
    llm_key = os.environ.get("LLM_API_KEY")
    if llm_key:
        parsers.append(ClaudeParser(llm_key, client=llm_client))

    dispatcher = Dispatcher(
        root=Path(root), authorized=autorizados, parsers=parsers,
        reports=reports, audit=AuditLog(conn),
    )
    return TelegramListener(token, dispatcher, client=client), ""


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


def _summarise_exit_errors(
    result: Any, notifier: Notifier | None, previous: set[str]
) -> None:
    """Un WARNING por ciclo con los errores que el motor NO notificó ya individualmente.

    El motor avisa solo (CRITICAL/WARNING) de las salidas rechazadas o falladas; resumirlas
    otra vez sería duplicar. Aquí solo quedan las silenciosas (p. ej. sin cotización). Sin
    nada que decir no se envía nada: una alerta que suena cada minuto acaba silenciada.

    Deduplicación: ``previous`` guarda los errores del ciclo anterior y solo se avisa de los
    NUEVOS (el mensaje lleva posición y símbolo, así que la clave es por posición). Un error
    que desaparece sale del conjunto, de modo que si vuelve se avisa otra vez. Se prefiere a
    un intervalo mínimo porque no hay reloj que afinar y avisa exactamente cuando la causa
    cambia, que es lo que el operador necesita saber.
    """
    errors = list(getattr(result, "errors", None) or ())
    already = list(getattr(result, "alerted", None) or ())
    pending = []
    for e in errors:
        if e in already:
            already.remove(e)  # multiconjunto: dos errores iguales no se compensan entre sí
        else:
            pending.append(e)
    new = [e for e in pending if e not in previous]
    previous.clear()
    previous.update(pending)
    if not new or notifier is None:
        return
    shown = "; ".join(new[:3]) + ("; ..." if len(new) > 3 else "")
    notifier.send(f"ciclo de salida con {len(new)} error(es) nuevo(s): {shown}", Level.WARNING)


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
    reported: set[str] = set()  # errores silenciosos ya avisados en el ciclo anterior
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
        _summarise_exit_errors(result, notifier, reported)

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
    # Control por Telegram en un hilo aparte. Es accesorio: si no esta configurado, el bot
    # opera igual y lo dice. Lo que no puede pasar es que la falta de chat impida operar.
    listener, motivo = build_chat_listener(
        cfg, root, quote_fn=deps.data_source.get_quote if deps.data_source is not None else None,
        client=deps.telegram_client,
    )
    chat_thread = None
    if listener is None:
        _say(cfg, f"control por Telegram desactivado: {motivo}")
    else:
        chat_thread = threading.Thread(target=listener.run_forever, name="telegram", daemon=True)
        chat_thread.start()
        _say(cfg, "control por Telegram activo: escribe /help al bot")

    _say(cfg, f"entrada: cron '{cfg.cadence.entry_cron}' (UTC); salida cada {cfg.cadence.exit_interval_seconds} s. Ctrl+C para parar")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        if listener is not None:
            listener.stop()
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


def _parse_aware(text: str, option: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        raise _fail(f"{option} no es una fecha ISO-8601: {text!r}", 2) from None
    if moment.tzinfo is None:
        # Sin zona horaria no hay forma de saber qué instante se pidió: no se adivina.
        raise _fail(
            f"{option} debe llevar zona horaria (p. ej. 2026-11-01T00:00:00+00:00): {text!r}", 2
        )
    return moment.astimezone(timezone.utc)


@app.command("fetch-quotes")
def fetch_quotes(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    symbol: list[str] = typer.Option(..., "--symbol", "-s", help="Símbolo (repetible), p. ej. BTC/USDT."),
    since: str = typer.Option(..., "--since", help="Inicio ISO-8601 con zona horaria."),
    out: Path = typer.Option(..., "--out", help="CSV de salida: timestamp,symbol,price."),
    exchange: str = typer.Option(DEFAULT_EXCHANGE, "--exchange", help="Exchange (ccxt) del que bajar velas."),
    timeframe: str = typer.Option("1h", "--timeframe", help="Temporalidad de las velas."),
    until: str | None = typer.Option(None, "--until", help="Fin ISO-8601 con zona horaria (por defecto, ahora)."),
) -> None:
    """Baja velas históricas y escribe el CSV de cotizaciones que lee el backtest.

    Códigos de salida: 0 todo bajado; 1 nada que escribir o fallo general; 2 argumentos
    inválidos; 3 descarga parcial (fichero escrito, pero faltan símbolos o quedó recortada).
    """
    deps = _deps(ctx)
    cfg = _load(config)
    now = (deps.now or (lambda: datetime.now(timezone.utc)))()
    start = _parse_aware(since, "--since")
    end = _parse_aware(until, "--until") if until is not None else now
    if start >= now:
        raise _fail("--since está en el futuro: no hay velas que bajar", 2)
    if start >= end:
        raise _fail("--since debe ser anterior a --until", 2)
    end = min(end, now)

    try:
        client = deps.history_client if deps.history_client is not None else make_history_client(exchange)
    except HistoryError as exc:
        raise _fail(f"error: {exc}", 2) from None
    try:
        client.load_markets()
    except Exception as exc:  # red, exchange caído: mensaje, no traza
        raise _fail(f"no se pudieron cargar los mercados de {exchange}: {exc}") from None
    try:
        validate_timeframe(client, timeframe)
    except HistoryError as exc:
        raise _fail(f"error: {exc}", 2) from None

    sleep = deps.sleep if deps.sleep is not None else time.sleep
    symbols = list(dict.fromkeys(s.strip() for s in symbol))
    _say(cfg, f"bajando {timeframe} de {exchange}: {', '.join(symbols)} ({start.isoformat()} -> {end.isoformat()})")

    quotes: list[Quote] = []
    problems = 0
    for sym in symbols:
        try:
            result = fetch_candles(
                client, sym, timeframe, start, end, now=now, sleep=sleep,
                page_limit=FETCH_PAGE_LIMIT, max_requests=FETCH_MAX_REQUESTS,
            )
        except SymbolNotFound:
            problems += 1
            _say(cfg, f"{sym}: no existe en {exchange}; se omite y se sigue con los demás")
            continue
        except HistoryError as exc:
            problems += 1
            _say(cfg, f"{sym}: falló la descarga ({exc}); se omite y se sigue con los demás")
            continue
        if not result.complete:
            problems += 1
            _say(
                cfg,
                f"{sym}: descarga incompleta, se alcanzó el tope de {FETCH_MAX_REQUESTS} peticiones; "
                "el histórico termina antes de lo pedido",
            )
        if not result.quotes:
            problems += 1
            _say(cfg, f"{sym}: el exchange no devolvió velas en ese rango")
            continue
        if result.skipped:
            _say(cfg, f"{sym}: {result.skipped} velas ilegibles descartadas")
        _say(cfg, f"{sym}: {len(result.quotes)} velas")
        quotes.extend(result.quotes)

    if not quotes:
        raise _fail(f"{mode_prefix(cfg.mode)} | no se bajó ninguna vela; no se escribe {out}")
    try:
        write_quotes_csv(out, quotes)
    except HistoryError as exc:
        raise _fail(f"{mode_prefix(cfg.mode)} | {exc}") from None
    first = min(q.timestamp for q in quotes)
    last = max(q.timestamp for q in quotes)
    _say(cfg, f"{len(quotes)} velas, de {first.isoformat()} a {last.isoformat()}, escritas en {out}")
    if problems:
        raise _fail(f"{mode_prefix(cfg.mode)} | descarga parcial: {problems} problema(s) arriba", 3)


_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def mints_for_symbols(symbols: Iterable[str], token_map: Mapping[str, str]) -> dict[str, str]:
    """``{simbolo: mint}`` de los simbolos de Solana que se pueden resolver.

    El simbolo de una senal es ``BASE/USDC``. La base es el mint cuando no habia mapeo (es lo
    que hace ``parse_swap``), o el nombre legible cuando si lo habia; por eso se mira tambien
    el mapeo a la inversa. Lo que no se resuelve se devuelve aparte para decirlo, nunca se
    adivina: bajar el precio del token equivocado seria peor que no bajarlo.
    """
    por_simbolo: dict[str, str] = {}
    inverso = {sym: mint for mint, sym in token_map.items()}
    for symbol in symbols:
        base = symbol.split("/", 1)[0].strip()
        mint = inverso.get(base) or (base if _MINT_RE.match(base) else None)
        if mint:
            por_simbolo[symbol] = mint
    return por_simbolo


@app.command("fetch-solana-quotes")
def fetch_solana_quotes(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    signals: Path = typer.Option(..., "--signals", help="CSV de senales del que sacar los tokens."),
    since: str = typer.Option(..., "--since", help="Inicio ISO-8601 con zona horaria."),
    out: Path = typer.Option(..., "--out", help="CSV de salida: timestamp,symbol,price."),
    token_map: Path | None = typer.Option(
        None, "--token-map", help="El MISMO mapeo que usaste en fetch-solana-signals."
    ),
    timeframe: str = typer.Option("hour", "--timeframe", help="hour, day o minute."),
) -> None:
    """Baja el historico de precios de los tokens de Solana que aparecen en un CSV de senales.

    Binance no cotiza BONK ni WIF, asi que fetch-quotes no sirve para ellos. Aqui los precios
    salen de GeckoTerminal, del pool con mas liquidez de cada token.

    Codigos de salida: 0 todo bajado; 1 nada que escribir o fallo general; 2 argumentos
    invalidos; 3 descarga parcial (fichero escrito, pero fallo algun token).
    """
    deps = _deps(ctx)
    cfg = _load(config)
    now = (deps.now or (lambda: datetime.now(timezone.utc)))()
    start = _parse_aware(since, "--since")
    if start >= now:
        raise _fail("--since esta en el futuro: no hay precios que bajar", 2)
    try:
        tokens = load_token_map(token_map) if token_map is not None else {}
    except HeliusError as exc:
        raise _fail(f"error: {exc}", 2) from None

    señales = _read_signals(signals)
    simbolos = sorted({s.symbol for s in señales})
    if not simbolos:
        raise _fail(f"{signals} no tiene ninguna senal de la que sacar tokens")

    por_simbolo = mints_for_symbols(simbolos, tokens)
    sin_resolver = [s for s in simbolos if s not in por_simbolo]
    if sin_resolver:
        _say(cfg, f"{len(sin_resolver)} simbolo(s) sin mint conocido, se omiten: {', '.join(sin_resolver[:5])}")
        _say(cfg, "  (usa --token-map con el mismo mapeo que en fetch-solana-signals)")
    if not por_simbolo:
        raise _fail("ningun simbolo se pudo resolver a un mint de Solana; no se escribe nada")

    client = deps.gecko_client if deps.gecko_client is not None else make_gecko_client()
    sleep = deps.sleep if deps.sleep is not None else time.sleep
    _say(cfg, f"bajando {timeframe} de {len(por_simbolo)} token(s) desde {start.isoformat()}")

    quotes: list[Quote] = []
    problemas = 0
    for i, (symbol, mint) in enumerate(sorted(por_simbolo.items())):
        if i:
            sleep(2.1)  # el plan gratuito de GeckoTerminal admite ~30 peticiones/minuto
        try:
            nuevas = fetch_token_quotes(
                client, mint, symbol, since=start, until=now,
                timeframe=timeframe, now=now, sleep=sleep,
            )
        except SolanaPriceError as exc:
            problemas += 1
            _say(cfg, f"{symbol}: fallo la descarga ({exc}); se sigue con los demas")
            continue
        _say(cfg, f"{symbol}: {len(nuevas)} velas")
        if not nuevas:
            problemas += 1
        quotes.extend(nuevas)

    if not quotes:
        raise _fail(f"{mode_prefix(cfg.mode)} | no se bajo ninguna cotizacion; no se escribe {out}")
    quotes.sort(key=lambda q: (q.timestamp, q.symbol))
    try:
        write_quotes_csv(out, quotes)
    except Exception as exc:  # noqa: BLE001
        raise _fail(f"{mode_prefix(cfg.mode)} | no se pudo escribir {out}: {type(exc).__name__}") from None
    _say(cfg, f"{len(quotes)} cotizaciones escritas en {out}")
    if problemas:
        raise _fail(f"{mode_prefix(cfg.mode)} | descarga parcial: {problemas} problema(s) arriba", 3)


@app.command("fetch-solana-signals")
def fetch_solana_signals(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    root: Path = RootOpt,
    wallet: list[str] = typer.Option(..., "--wallet", "-w", help="Wallet de Solana a seguir (repetible)."),
    since: str = typer.Option(..., "--since", help="Inicio ISO-8601 con zona horaria."),
    out: Path = typer.Option(..., "--out", help="CSV de salida: timestamp,trader_id,symbol,side,amount_usd,signature."),
    token_map: Path | None = typer.Option(
        None, "--token-map", help="YAML/JSON opcional con 'mint: SÍMBOLO' para los tokens que sigues."
    ),
    sol_quotes: Path | None = typer.Option(
        None, "--sol-quotes",
        help="CSV de cotizaciones (timestamp,symbol,price) con SOL/USDT para valorar los swaps "
             "contra SOL. Sin él se omiten: en Solana el 80 % de los swaps no tocan stablecoins.",
    ),
) -> None:
    """Lee los swaps de unas wallets de Solana vía Helius y escribe el CSV de señales del backtest.

    La clave va en HELIUS_API_KEY (entorno o .env de --root), nunca en el YAML. Solo se escriben
    los swaps valorables en dólares; el resto se omite y se cuenta por motivo.

    Un swap es valorable si lleva una stablecoin (importe exacto) o, con --sol-quotes, si va
    contra SOL (importe estimado con el precio de SOL de esa hora). Baja esas cotizaciones con
    'fetch-quotes --symbol SOL/USDT' cubriendo el mismo periodo que --since.

    Códigos de salida: 0 todo leído; 1 nada que escribir o fallo general; 2 argumentos o clave
    inválidos; 3 lectura parcial (fichero escrito, pero falló una wallet o quedó recortada).
    """
    deps = _deps(ctx)
    cfg = _load(config)
    now = (deps.now or (lambda: datetime.now(timezone.utc)))()
    start = _parse_aware(since, "--since")
    if start >= now:
        raise _fail("--since está en el futuro: no hay swaps que leer", 2)
    try:
        wallets = [validate_wallet(w.strip()) for w in dict.fromkeys(wallet)]
        api_key = load_api_key(os.environ, root)
        tokens = load_token_map(token_map) if token_map is not None else {}
    except HeliusError as exc:
        raise _fail(f"error: {exc}", 2) from None

    sol_price = None
    if sol_quotes is not None:
        # Se construye ANTES de pedir nada a Helius: un CSV sin cotizaciones de SOL haría
        # cientos de peticiones para omitir luego todos los swaps por 'no_sol_price'.
        try:
            table = SolPriceTable(_read_quotes(sol_quotes))
        except SolPriceError as exc:
            raise _fail(f"error en las cotizaciones de SOL {sol_quotes}: {exc}", 2) from None
        if not len(table):
            raise _fail(
                f"{sol_quotes} no trae ninguna cotización de SOL (se buscan los pares "
                f"{', '.join(SOL_SYMBOLS)}); bájalas con 'fetch-quotes --symbol SOL/USDT'",
                2,
            )
        sol_price = table.price_at
        _say(cfg, f"{len(table)} cotizaciones de SOL cargadas de {sol_quotes}")
    else:
        _say(cfg, "sin --sol-quotes: los swaps contra SOL se omitirán (la mayoría, en Solana)")

    client = deps.solana_client if deps.solana_client is not None else make_solana_client()
    sleep = deps.sleep if deps.sleep is not None else time.sleep
    _say(cfg, f"leyendo swaps de {len(wallets)} wallet(s) desde {start.isoformat()}")

    signals: list = []
    omitted: dict[str, int] = {}
    signatures: dict[str, str] = {}
    problems = 0
    for address in wallets:
        try:
            scan = fetch_wallet_swaps(
                client, address, api_key, since=start, token_map=tokens,
                sol_price=sol_price, sleep=sleep,
            )
        except HeliusOrderError as exc:
            # Integridad de datos, no lectura parcial: no se escribe nada y no se sigue.
            raise _fail(f"{mode_prefix(cfg.mode)} | {address}: {exc}; no se escribe {out}") from None
        except HeliusError as exc:
            problems += 1
            _say(cfg, f"{address}: falló la lectura ({exc}); se sigue con las demás")
            continue
        if not scan.complete:
            problems += 1
            why = (
                "el servidor ignoró el cursor de paginación (página llena repetida)"
                if scan.stop == "cursor_stalled"
                else "se alcanzó el tope de peticiones"
            )
            _say(cfg, f"{address}: lectura incompleta, {why}; el historial termina antes de lo pedido")
        _say(cfg, f"{address}: {len(scan.signals)} señales")
        signals.extend(scan.signals)
        signatures.update(scan.signatures)
        for reason, n in scan.omitted.items():
            omitted[reason] = omitted.get(reason, 0) + n

    total_omitted = sum(omitted.values())
    if total_omitted:
        _say(cfg, f"{total_omitted} swaps omitidos por no poder valorarse en dólares:")
        for reason, n in sorted(omitted.items()):
            _say(cfg, f"  {reason}: {n} ({OMISSION_REASONS.get(reason, reason)})")
    if not signals:
        raise _fail(f"{mode_prefix(cfg.mode)} | no hay ninguna señal que escribir; no se escribe {out}")
    signals.sort(key=lambda s: (s.timestamp, s.id))
    try:
        write_signals_csv(out, signals, signatures)
    except HeliusError as exc:
        raise _fail(f"{mode_prefix(cfg.mode)} | {exc}") from None
    _say(cfg, f"{len(signals)} señales escritas en {out}")
    if problems:
        raise _fail(f"{mode_prefix(cfg.mode)} | lectura parcial: {problems} problema(s) arriba", 3)


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
