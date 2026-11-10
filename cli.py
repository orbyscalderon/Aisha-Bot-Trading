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

import ccxt
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
from adapters.cex.executor import CexExecutor
from adapters.cex.source import CexCsvSignalSource, CexDataSource, make_exchange_client
from adapters.env import load_dotenv
from adapters.solana.prices import (
    JupiterPriceSource,
    LiveSolPrice,
    SolanaPriceError,
    fetch_token_quotes,
)
from adapters.solana.discover import (
    DEFAULT_MAX_POOLS as DISCOVER_MAX_POOLS,
    DEFAULT_MIN_LIQUIDITY_USD as DISCOVER_MIN_LIQUIDITY_USD,
    DEFAULT_MIN_POOLS as DISCOVER_MIN_POOLS,
    DEFAULT_MIN_TRADE_USD as DISCOVER_MIN_TRADE_USD,
    WalletHistory,
    discover_wallets,
    format_history,
    format_lead,
    merge_history,
)
from adapters.solana.screener import format_profile, profile_wallet
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
    SolanaWalletSignalSource,
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
from ports.market import MarketDataSource, OrderExecutor
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
    help="Bot de copy-trading: backtest, simulado (paper) y real contra un CEX (live, testnet por defecto).",
    no_args_is_help=True,
    add_completion=False,
)


class InvalidExchange(Exception):
    """El exchange pedido no existe en ccxt (única causa esperada de fallo al construir la fuente)."""


class LiveModeNotImplemented(RuntimeError):
    """Queda para los mercados cuya ejecución real aún no existe (Solana, acciones)."""


class MissingExchangeCredentials(Exception):
    """Modo live sin claves del exchange: no hay con qué operar."""


EXCHANGE_KEY_VAR = "EXCHANGE_API_KEY"
EXCHANGE_SECRET_VAR = "EXCHANGE_API_SECRET"


def _exchange_credentials() -> tuple[str, str]:
    """Claves del exchange desde el entorno. Nunca del YAML: ese fichero se versiona.

    Devuelve las dos o falla; con media clave no se opera. El mensaje dice qué falta
    sin decir qué hay: un error que imprimiera la clave la dejaría en los logs.
    """
    clave = (os.environ.get(EXCHANGE_KEY_VAR) or "").strip()
    secreto = (os.environ.get(EXCHANGE_SECRET_VAR) or "").strip()
    faltan = [n for n, v in ((EXCHANGE_KEY_VAR, clave), (EXCHANGE_SECRET_VAR, secreto)) if not v]
    if faltan:
        raise MissingExchangeCredentials(
            f"modo live sin {' ni '.join(faltan)}: ponlos en el .env "
            "(o como variables de entorno en la nube) y vuelve a arrancar"
        )
    return clave, secreto


@dataclass
class CliDeps:
    """Dependencias inyectables en ``ctx.obj``; ``None`` significa "construir la real"."""

    scheduler: Any = None
    data_source: MarketDataSource | None = None
    executor: OrderExecutor | None = None  # el real lo elige el modo (paper o live)
    notifier: Notifier | None = None
    history_client: Any = None  # cliente ccxt de fetch-quotes
    sleep: Callable[[float], None] | None = None  # temporizador de fetch-quotes
    now: Callable[[], datetime] | None = None  # reloj de fetch-quotes y fetch-solana-signals
    solana_client: Any = None  # cliente httpx de fetch-solana-signals
    telegram_client: Any = None  # cliente httpx del control por Telegram
    gecko_client: Any = None  # cliente httpx de fetch-solana-quotes
    jupiter_client: Any = None  # cliente httpx de los precios en vivo de Solana


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
    executor: OrderExecutor | None = None,
    notifier: Notifier | None = None,
    paper_capital: Decimal = DEFAULT_CAPITAL,
) -> tuple[Engine, Notifier, AuditLog]:
    en_vivo = config.mode is Mode.LIVE
    cliente: Any = None
    # La fuente de datos va ANTES de tocar el disco: un exchange inválido no deja una base
    # de datos creada ni una conexión colgando. Solo se captura el error esperado.
    if data_source is not None:
        raw_data = data_source
    elif en_vivo:
        clave, secreto = _exchange_credentials()
        try:
            cliente = make_exchange_client(
                exchange_id, clave, secreto, testnet=config.cex.testnet
            )
        except ValueError as exc:
            raise InvalidExchange(f"exchange no válido {exchange_id!r}: {exc}") from exc
        # Cargar los mercados al arrancar, no al primer ciclo: de aquí sale la precisión
        # de cantidad que usa el ejecutor para redondear las ventas. Sin ella redondearía
        # con el valor de respaldo y el exchange podría tumbar un stop-loss por lote.
        cliente.load_markets()
        raw_data = CexDataSource.from_client(cliente, ignore_assets=config.cex.ignore_assets)
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
    exec_cfg = config.execution[Market.CEX]
    if en_vivo:
        # Capital REAL de la cuenta, sin envolver: en vivo el dinero es el que hay,
        # no el que dice un número de configuración.
        data = raw_data
        if executor is None:
            if cliente is None:
                # Pasa si alguien inyecta una fuente de datos en modo live sin inyectar
                # ejecutor. Sin cliente no hay con qué operar, y un CexExecutor envolviendo
                # None construiría sin quejarse y fallaría en la primera orden de verdad.
                raise LiveModeNotImplemented(
                    "modo live con una fuente de datos inyectada necesita también un "
                    "ejecutor: sin cliente del exchange no hay forma de mandar órdenes"
                )
            executor = CexExecutor(cliente, quantity_precision=exec_cfg.quantity_precision)
    else:
        data = PaperCapitalSource(raw_data, repo, paper_capital)
        if executor is None:
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


def _load_env(root: Path | str) -> None:
    """Vuelca el ``.env`` de ``root`` en el entorno, sin pisar lo que ya viniera de fuera.

    Va al PRINCIPIO de todo comando que lea claves. Antes solo ``HELIUS_API_KEY`` se leía del
    fichero, así que la tarea programada —que no tiene a nadie que exporte variables— arrancaba
    el bot sin Telegram y sin avisar.
    """
    load_dotenv(root)


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
    solana_wallet: list[str] = typer.Option(
        [], "--solana-wallet", "-w",
        help="Wallet de Solana a copiar EN VIVO (repetible). Alternativa a --signals: "
             "lee los swaps de Helius en cada ciclo en vez de un CSV estático.",
    ),
    root: Path = RootOpt,
    once: bool = typer.Option(False, "--once", help="Un ciclo de entrada y uno de salida, y termina."),
    exchange: str = typer.Option(DEFAULT_EXCHANGE, "--exchange", help="Exchange (ccxt) de las cotizaciones."),
    capital: str = typer.Option(str(DEFAULT_CAPITAL), "--capital", help="Capital simulado en USD (ignorado en live: ahí manda el saldo real)."),
    dinero_real: bool = typer.Option(
        False, "--dinero-real",
        help="Confirma operar con DINERO REAL. Solo tiene efecto con mode: live y testnet: false.",
    ),
) -> None:
    """Ejecuta el bot: simulado (paper) o real (live) contra el exchange."""
    deps = _deps(ctx)
    _load_env(root)
    cfg = _load(config)
    if cfg.mode is Mode.LIVE and not cfg.cex.testnet and not dinero_real:
        # Operar con dinero de verdad exige DOS actos deliberados e independientes:
        # escribir `testnet: false` en el YAML (que se versiona, así que queda en el
        # historial) y pasar esta bandera al arrancar. Con un solo interruptor, un
        # fichero copiado de otro sitio bastaría para empezar a gastar sin querer.
        raise _fail(
            f"{mode_prefix(cfg.mode)} | la configuración dice testnet: false, es decir DINERO "
            "REAL en el exchange. No se ha ejecutado nada. Si es lo que quieres, añade "
            "--dinero-real; si no, pon testnet: true.",
            3,
        )
    if signals is None and not solana_wallet:
        raise _fail(
            "falta la fuente de señales: usa --signals con un CSV, o --solana-wallet "
            "para copiar wallets en vivo", 2,
        )
    if signals is not None and solana_wallet:
        # Mezclarlas silenciosamente daría dos cursores sobre la misma base y nadie sabría
        # de dónde salió cada posición.
        raise _fail("usa --signals o --solana-wallet, no las dos a la vez", 2)
    try:
        entry_trigger = CronTrigger.from_crontab(cfg.cadence.entry_cron, timezone=timezone.utc)
    except ValueError as exc:
        raise _fail(f"error de configuración: cadence.entry_cron inválido ({cfg.cadence.entry_cron!r}): {exc}", 2) from None

    # Validar el CSV AL ARRANCAR. `CexCsvSignalSource` lo lee perezosamente dentro del
    # ciclo, así que un fichero ausente o malformado saldría como traza cruda a mitad de
    # ejecución en vez de como mensaje al empezar.
    if signals is not None:
        _read_signals(signals)

    # La fuente en vivo se monta ANTES de tocar el disco: una wallet mal escrita o una
    # clave ausente deben parar aquí, no con media base de datos creada.
    live_source = None
    live_data = None
    if solana_wallet:
        try:
            wallets = [validate_wallet(w.strip()) for w in dict.fromkeys(solana_wallet)]
            api_key = load_api_key(os.environ, root)
        except HeliusError as exc:
            raise _fail(f"error: {exc}", 2) from None
        # Jupiter sirve las DOS cosas: el precio de los tokens para el motor y el de SOL
        # para valorar los swaps. Una fuente menos que pueda discrepar consigo misma.
        # El MISMO reloj que el resto del comando. Con el reloj real aquí dentro, un swap
        # recién leído parecería viejísimo en cuanto el reloj del motor no fuese el de la
        # máquina, y se descartaría por antigüedad sin que nada lo explicase.
        reloj = deps.now or (lambda: datetime.now(timezone.utc))
        live_data = JupiterPriceSource({}, client=deps.jupiter_client, now=reloj)
        live_source = SolanaWalletSignalSource(
            wallets, api_key, client=deps.solana_client,
            sol_price=LiveSolPrice(live_data, now=reloj),
            sleep=deps.sleep if deps.sleep is not None else time.sleep,
        )

    # Decirlo SIEMPRE y sin ambigüedad. Confundir testnet con dinero real en cualquiera
    # de los dos sentidos es la peor equivocación posible con este programa.
    if cfg.mode is Mode.LIVE:
        donde = f"testnet de {exchange}" if cfg.cex.testnet else f"DINERO REAL en {exchange}"
        _say(cfg, f"arrancando EN VIVO contra {donde} (config {cfg.fingerprint()[:12]})")
    else:
        _say(cfg, f"arrancando en simulado (config {cfg.fingerprint()[:12]})")
    try:
        engine, notifier, audit = _build(
            cfg, root, exchange_id=exchange,
            data_source=deps.data_source if deps.data_source is not None else live_data,
            executor=deps.executor, notifier=deps.notifier,
            paper_capital=_parse_capital(capital),
        )
    except InvalidExchange as exc:
        raise _fail(str(exc), 2) from None
    except MissingExchangeCredentials as exc:
        raise _fail(str(exc), 2) from None
    except ccxt.BaseError as exc:
        # Claves inválidas, exchange caído o sin red al cargar los mercados. Parar aquí
        # con un mensaje: un bot que arranca sin poder operar es peor que uno que no arranca.
        raise _fail(f"no se pudo conectar con {exchange}: {exc}", 2) from None
    warn_if_no_protections(cfg, notifier)
    warn_if_copy_sell_primary(cfg, notifier)
    source = live_source if live_source is not None else CexCsvSignalSource(signals)
    if live_source is not None:
        _say(cfg, f"copiando EN VIVO {len(live_source.list_followed())} wallet(s) de Solana")
    entry_job, exit_job = build_jobs(
        engine, source, notifier=notifier, audit=audit,
        lock_timeout=exit_lock_timeout(cfg.cadence.exit_interval_seconds),
    )

    if once:
        # El reloj inyectado, como en el resto de comandos. En producción deps.now es None y
        # esto es datetime.now(); usarlo directamente dejaba --once sin forma de probarse con
        # un reloj simulado, que es justo lo que hace falta para verificar el cursor.
        now = (deps.now or (lambda: datetime.now(timezone.utc)))()
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


@app.command("telegram-chat-id")
def telegram_chat_id(
    ctx: typer.Context, config: Path = ConfigOpt, root: Path = RootOpt
) -> None:
    """Dice tu chat_id de Telegram, que es lo que va en TELEGRAM_ALLOWED_CHAT_IDS.

    Es el paso que atasca a todo el mundo: el token identifica al BOT, pero para que el bot te
    hable hace falta el numero de TU conversacion con el, y Telegram solo lo revela despues de
    que le escribas. Manda cualquier mensaje al bot y ejecuta esto.

    No cambia nada: solo lee y muestra.

    Codigos de salida: 0 hay algun chat; 1 ninguno todavia; 2 falta el token o no vale.
    """
    _load_env(root)
    cfg = _load(config)
    deps = _deps(ctx)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise _fail("falta TELEGRAM_BOT_TOKEN en el entorno o en .env", 2)

    import httpx
    from adapters.notify.telegram import _API, _scrub
    client = deps.telegram_client if deps.telegram_client is not None else httpx.Client(timeout=20.0)
    try:
        yo = client.get(f"{_API}/bot{token}/getMe")
        if yo.status_code == 401:
            raise _fail("Telegram rechazo el token (HTTP 401): revisa TELEGRAM_BOT_TOKEN", 2)
        if yo.status_code != 200:
            raise _fail(f"Telegram respondio HTTP {yo.status_code} a getMe", 2)
        nombre = ((yo.json() or {}).get("result") or {}).get("username") or "?"
        _say(cfg, f"bot @{nombre}: el token es valido")
        r = client.get(f"{_API}/bot{token}/getUpdates", params={"limit": 50})
        if r.status_code != 200:
            raise _fail(f"Telegram respondio HTTP {r.status_code} al pedir mensajes", 2)
        updates = ((r.json() or {}).get("result")) or []
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fail(f"fallo hablando con Telegram: {type(exc).__name__}: "
                    f"{_scrub(str(exc), token)[:120]}", 2) from None

    chats: dict[Any, tuple[str, str]] = {}
    for u in updates:
        mensaje = (u or {}).get("message") or (u or {}).get("edited_message") or {}
        chat = mensaje.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = (
                str(chat.get("type") or "?"),
                str(chat.get("username") or chat.get("first_name") or chat.get("title") or "?"),
            )
    if not chats:
        raise _fail(
            f"nadie ha escrito a @{nombre} todavia. Abrelo en Telegram, pulsa Start o mandale "
            "cualquier mensaje, y vuelve a ejecutar esto"
        )

    typer.echo("")
    for cid, (tipo, quien) in chats.items():
        typer.echo(f"  chat_id {cid}   ({tipo}, de {quien})")
    typer.echo("")
    typer.echo("Pon esto en tu .env:")
    typer.echo(f"  TELEGRAM_ALLOWED_CHAT_IDS={','.join(str(c) for c in chats)}")
    typer.echo(f"  TELEGRAM_CHAT_ID={next(iter(chats))}")


@app.command("screen-solana-wallets")
def screen_solana_wallets(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    root: Path = RootOpt,
    wallet: list[str] = typer.Option(..., "--wallet", "-w", help="Wallet a examinar (repetible)."),
    since: str = typer.Option(..., "--since", help="Inicio ISO-8601 con zona horaria."),
    sol_quotes: Path | None = typer.Option(
        None, "--sol-quotes", help="CSV con SOL/USDT, para valorar los swaps contra SOL."
    ),
    max_requests: int = typer.Option(
        30, "--max-requests", min=1, help="Tope de peticiones a Helius POR wallet."
    ),
) -> None:
    """Mide si unas wallets son traders antes de copiarlas, y descarta las que no lo son.

    Responde a '¿es un trader o un bot de infraestructura? ¿mueve cantidades que valga la pena
    copiar?'. NO responde a '¿habria ganado dinero copiarlo?': eso depende de TUS umbrales y lo
    contesta el backtest.

    Codigos de salida: 0 alguna wallet pasa la criba; 1 ninguna; 2 argumentos invalidos.
    """
    deps = _deps(ctx)
    _load_env(root)
    cfg = _load(config)
    now = (deps.now or (lambda: datetime.now(timezone.utc)))()
    start = _parse_aware(since, "--since")
    if start >= now:
        raise _fail("--since esta en el futuro", 2)
    try:
        wallets = [validate_wallet(w.strip()) for w in dict.fromkeys(wallet)]
        api_key = load_api_key(os.environ, root)
    except HeliusError as exc:
        raise _fail(f"error: {exc}", 2) from None

    sol_price = None
    if sol_quotes is not None:
        try:
            tabla = SolPriceTable(_read_quotes(sol_quotes))
        except SolPriceError as exc:
            raise _fail(f"error en las cotizaciones de SOL: {exc}", 2) from None
        if not len(tabla):
            raise _fail(f"{sol_quotes} no trae ninguna cotizacion de SOL", 2)
        sol_price = tabla.price_at
    else:
        _say(cfg, "sin --sol-quotes: los swaps contra SOL no se valoran (la mayoria, en Solana)")

    client = deps.solana_client if deps.solana_client is not None else make_solana_client()
    sleep = deps.sleep if deps.sleep is not None else time.sleep
    _say(cfg, f"examinando {len(wallets)} wallet(s) desde {start.isoformat()}")

    candidatos = 0
    for address in wallets:
        try:
            scan = fetch_wallet_swaps(
                client, address, api_key, since=start, sol_price=sol_price,
                sleep=sleep, max_requests=max_requests,
            )
        except HeliusError as exc:
            typer.echo(f"{address}\n  no se pudo leer: {exc}\n")
            continue
        leidos = len(scan.signals) + sum(scan.omitted.values())
        perfil = profile_wallet(address, scan.signals, leidos, scan.omitted, scan.complete)
        sirve, _ = perfil.verdict()
        candidatos += 1 if sirve else 0
        typer.echo(format_profile(perfil))
        typer.echo("")

    _say(cfg, f"{candidatos} de {len(wallets)} pasan la criba")
    if not candidatos:
        raise _fail("ninguna wallet pasa la criba; no hay candidato que copiar")


#: Columnas del historial de barridos. Es un CSV y no un formato propio para que se pueda
#: abrir en cualquier sitio y comprobar a mano lo que el comando dice haber visto.
_HISTORIAL_COLUMNAS = ("wallet", "barridos", "primera", "ultima", "mediana_usd", "pools")


def _read_history(path: Path) -> list[WalletHistory]:
    """Historial de barridos previos, o vacio si aun no existe.

    Una fila ilegible PARA el comando en vez de ignorarse: perder barridos acumulados en
    silencio haria que 'visto en 5 barridos' significara otra cosa sin que nadie se entere.
    """
    if not path.exists():
        return []
    salida: list[WalletHistory] = []
    with path.open(newline="", encoding="utf-8") as fh:
        lector = csv.DictReader(fh)
        if lector.fieldnames is None or tuple(lector.fieldnames) != _HISTORIAL_COLUMNAS:
            raise ValueError(
                f"cabecera inesperada: se esperaba {','.join(_HISTORIAL_COLUMNAS)}"
            )
        for numero, fila in enumerate(lector, start=2):
            try:
                salida.append(WalletHistory(
                    wallet=validate_wallet((fila["wallet"] or "").strip()),
                    barridos=int(fila["barridos"]),
                    primera=_parse_aware(fila["primera"], "primera"),
                    ultima=_parse_aware(fila["ultima"], "ultima"),
                    mediana_usd=Decimal(fila["mediana_usd"]),
                    pools=tuple(p for p in (fila["pools"] or "").split("|") if p),
                ))
            except (HeliusError, InvalidOperation, KeyError, TypeError, ValueError,
                    typer.Exit) as exc:
                raise ValueError(f"fila {numero} ilegible: {exc}") from None
    return salida


def _write_history(path: Path, historial: Iterable[WalletHistory]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        escritor = csv.writer(fh)
        escritor.writerow(_HISTORIAL_COLUMNAS)
        for h in historial:
            escritor.writerow([
                h.wallet, h.barridos, h.primera.isoformat(), h.ultima.isoformat(),
                f"{h.mediana_usd:.2f}", "|".join(h.pools),
            ])


@app.command("discover-solana-wallets")
def discover_solana_wallets(
    ctx: typer.Context,
    config: Path = ConfigOpt,
    out: Path | None = typer.Option(
        None, "--out", help="Fichero donde escribir las direcciones, una por linea."
    ),
    max_pools: int = typer.Option(
        DISCOVER_MAX_POOLS, "--max-pools", min=1,
        help="Pools a recorrer. Cada uno es una peticion y el plan gratuito da ~30 por minuto.",
    ),
    min_trade_usd: str = typer.Option(
        str(DISCOVER_MIN_TRADE_USD), "--min-operacion",
        help="Importe minimo de un intercambio para tenerlo en cuenta, en USD.",
    ),
    min_pools: int = typer.Option(
        DISCOVER_MIN_POOLS, "--min-pools", min=1,
        help="Pools distintos en los que hay que haber visto a la wallet.",
    ),
    min_liquidity: str = typer.Option(
        str(DISCOVER_MIN_LIQUIDITY_USD), "--min-liquidez",
        help="Liquidez minima del pool en USD.",
    ),
    historial: Path | None = typer.Option(
        None, "--historial",
        help="CSV que ACUMULA barridos. Repite el comando cada pocas horas contra el mismo "
             "fichero: quien siga apareciendo dias despues es el candidato de verdad.",
    ),
    min_barridos: int = typer.Option(
        1, "--min-barridos", min=1,
        help="Solo lista (y escribe en --out) a quien se haya visto en al menos N barridos. "
             "Necesita --historial.",
    ),
    limit: int = typer.Option(25, "--limit", min=1, help="Cuantas direcciones mostrar."),
) -> None:
    """Busca wallets que esten operando AHORA en los pools con liquidez de Solana.

    Es el paso que faltaba delante de 'screen-solana-wallets': esa sabe decir si una wallet
    sirve, pero habia que darle las direcciones a mano. Aqui salen de los intercambios reales.

    NO decide nada. Devuelve direcciones PLAUSIBLES, por dos razones concretas: la ventana que
    publica GeckoTerminal puede ser de minutos (un ritmo calculado sobre eso daria miles de
    operaciones al dia, que es un artefacto de la division) y solo se ve una cara del pool, no
    la cartera. Quien juzga es la criba, con el historial completo de Helius.

    UN SOLO BARRIDO SELECCIONA BOTS, por construccion: mira los ultimos ~300 intercambios de
    cada pool, que en los activos son minutos, asi que quien opera dos veces al dia no aparece
    casi nunca y quien opera sin parar aparece siempre. Eso NO se arregla afinando umbrales.
    Se arregla con --historial: repite el barrido cada pocas horas contra el mismo fichero y
    filtra por --min-barridos. "Visto en 5 barridos de 5 dias" no se puede fingir operando
    rapido; hay que seguir ahi al dia siguiente.

    Usa GeckoTerminal: gratis y sin clave. No gasta peticiones de Helius.

    El flujo util es:
      # cada pocas horas (p. ej. una tarea programada)
      python -m cli discover-solana-wallets --historial barridos.csv
      # cuando haya varios dias acumulados
      python -m cli discover-solana-wallets --historial barridos.csv --min-barridos 5 \
          --out candidatas.txt
      python -m cli screen-solana-wallets -w <direccion> --since <fecha> --sol-quotes <csv>

    Codigos de salida: 0 hay direcciones; 1 ninguna; 2 argumentos invalidos.
    """
    deps = _deps(ctx)
    cfg = _load(config)
    try:
        minimo = Decimal(min_trade_usd)
        liquidez = Decimal(min_liquidity)
    except InvalidOperation:
        raise _fail("--min-operacion y --min-liquidez tienen que ser numeros", 2) from None
    if minimo <= 0 or liquidez <= 0:
        raise _fail("--min-operacion y --min-liquidez tienen que ser mayores que cero", 2)
    if min_barridos > 1 and historial is None:
        # Sin historial, "visto en 3 barridos" no se puede saber: solo hay uno. Decirlo es
        # mejor que filtrar por un numero que siempre vale 1 y devolver una lista vacia.
        raise _fail("--min-barridos necesita --historial: con un solo barrido todos valen 1", 2)

    now = (deps.now or (lambda: datetime.now(timezone.utc)))()
    client = deps.gecko_client if deps.gecko_client is not None else make_gecko_client()
    sleep = deps.sleep if deps.sleep is not None else time.sleep
    _say(cfg, f"recorriendo hasta {max_pools} pools con mas de {liquidez} USD de liquidez")
    try:
        leads, recuento = discover_wallets(
            client, max_pools=max_pools, min_trade_usd=minimo, min_pools=min_pools,
            min_liquidity_usd=liquidez, sleep=sleep,
        )
    except SolanaPriceError as exc:
        raise _fail(f"error: {exc}", 2) from None

    typer.echo(
        f"{recuento['intercambios']} intercambios de mas de {minimo} USD en "
        f"{recuento['pools']} pools"
        + (f" ({recuento['pools_fallidos']} no se pudieron leer)"
           if recuento["pools_fallidos"] else "")
    )
    if not leads:
        raise _fail(
            "ninguna direccion aparece en al menos "
            f"{min_pools} pools con operaciones de {minimo} USD. Baja --min-operacion o "
            "--min-pools, o sube --max-pools"
        )

    typer.echo("")
    elegidas = [l.wallet for l in leads]

    if historial is None:
        for lead in leads[:limit]:
            typer.echo(format_lead(lead))
    else:
        try:
            previos = _read_history(historial)
        except ValueError as exc:
            raise _fail(f"error leyendo {historial}: {exc}", 2) from None
        acumulado = merge_history(previos, leads, now)
        _write_history(historial, acumulado)
        persistentes = [h for h in acumulado if h.barridos >= min_barridos]
        typer.echo(
            f"historial: {len(acumulado)} direcciones en total, "
            f"{len(persistentes)} vistas en {min_barridos}+ barridos"
        )
        typer.echo("")
        for h in persistentes[:limit]:
            typer.echo(format_history(h))
        elegidas = [h.wallet for h in persistentes]
        if min_barridos == 1 and max(( h.barridos for h in acumulado), default=0) < 2:
            typer.echo("")
            typer.echo(
                "Este es el primer barrido, asi que todos valen 1 y la lista no dice nada aun. "
                "Repitelo cada pocas horas y filtra con --min-barridos."
            )
    typer.echo("")

    if out is not None:
        # Solo las direcciones: el fichero esta hecho para encadenarlo con la criba, no para
        # leerlo. Lo que se mide aqui no sirve para decidir y guardarlo invitaria a usarlo.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(elegidas) + "\n", encoding="utf-8")
        typer.echo(f"{len(elegidas)} direcciones en {out}")

    _say(
        cfg,
        f"{len(elegidas)} direcciones por examinar. NINGUNA esta aprobada: pasalas por "
        "'screen-solana-wallets' antes de copiar a nadie",
    )


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
    _load_env(root)
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
    _load_env(root)
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
    _load_env(root)
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
