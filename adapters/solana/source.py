"""Lector de wallets de Solana vía Helius: swaps reales convertidos en señales de copy-trading.

Usa la API de transacciones enriquecidas
``GET https://api.helius.xyz/v0/addresses/{wallet}/transactions?type=SWAP&api-key=...&before=<firma>``.

FORMA DE RESPUESTA ASUMIDA (no se ha probado contra Helius real; ver el informe). Una lista JSON,
de la más nueva a la más antigua, de objetos con::

    signature          str, no vacía             firma de la transacción (única e inmutable)
    timestamp          int, segundos unix        momento de la transacción
    transactionError   null si salió bien        (cualquier otro valor = transacción fallida)
    events.swap        objeto, o ausente         el swap ya interpretado por Helius:
        tokenInputs    [leg]   lo que SALE de la wallet     (lista, puede faltar o ir vacía)
        tokenOutputs   [leg]   lo que ENTRA en la wallet
        nativeInput    {account, amount} | null   SOL nativo que sale (lamports)
        nativeOutput   {account, amount} | null   SOL nativo que entra
    leg = {userAccount, mint, rawTokenAmount: {tokenAmount: "<unidades mínimas>", decimals: int},
           symbol?: str}                    # ``symbol`` es opcional y no se da por existente

Reglas ante lo que no encaja:

- Forma rota (no es lista, una transacción sin firma/hora, un importe no numérico, decimales que
  no son enteros...) -> ``HeliusError`` con un mensaje claro. Nunca se rellena ni se adivina.
- Forma válida pero no valorable (token->token, un swap sin evento, una transacción fallida...) ->
  se OMITE y se cuenta por motivo en ``SwapScan.omitted``. No se inventa ningún precio.

Decisiones:

- Compra = stablecoin -> token; venta = token -> stablecoin. ``amount_usd`` es el lado de la
  stablecoin (1 USDC = 1 USD), el único que se conoce en dólares sin pedir precios. Las
  stablecoins se reconocen por la DIRECCIÓN DEL MINT, nunca por el símbolo (cualquiera puede
  acuñar un token que se llame "USDC").
- Los importes vienen en unidades mínimas: ``Decimal(str(valor)).scaleb(-decimals)``. Nunca float.
  La suposición se autoverifica: las unidades mínimas son SIEMPRE enteras, así que un
  ``tokenAmount`` con punto decimal, exponente o signo (lo que mandaría Helius si usara unidades
  de interfaz) lanza ``HeliusError`` en vez de dar un importe falso por un factor 10^decimals.
- SOL: la pata nativa (lamports, 9 decimales) o wSOL de la wallet cuenta como token del swap
  solo si llega a ``DUST_SOL``; por debajo se ignora (alquiler y restos de las rutas Jupiter).
  Su símbolo por defecto es ``SOL``. Se exige que cada página venga de más nueva a más antigua
  (si no, ``since`` cortaría pronto y el resultado saldría truncado en silencio): si no, error.
- ``Signal.id`` deriva de la firma (más la wallet, porque una firma puede afectar a dos wallets
  seguidas). Ese id alimenta el ``client_order_id`` que impide comprar dos veces la misma señal:
  no puede depender del orden de lectura, del importe ni de la hora de la descarga.
- Símbolo: mapeo del usuario > ``symbol`` que traiga Helius (si es sano) > dirección del mint.
  Un símbolo vacío o inventado rompería el emparejamiento con los precios más adelante. Ojo: el
  ``symbol`` que trae el propio token es autodeclarado y un token falso puede llamarse "BONK";
  para los tokens que se siguen de cerca conviene fijarlos en el mapeo.
- Paginación con ``before`` = firma de la última transacción de la página. Para si la página
  viene vacía, si ya no trae nada nuevo (cursor sin avanzar), si llega a antes de ``since``, o al
  tope duro de peticiones (los reintentos también cuentan); el tope se reporta como
  ``complete=False`` porque un histórico recortado en silencio engaña.
- La clave de API viaja en la URL (la documentación de Helius solo describe el parámetro
  ``api-key``; no se pudo confirmar autenticación por cabecera). httpx escribe la URL completa a
  nivel INFO en cada petición: la fábrica de registros limpia ``api-key=...`` en todo registro de
  ``httpx``/``httpcore`` y de sus loggers hijos al crearlo, y los mensajes de error de este módulo
  pasan por ``_scrub``. No cubre a un cliente con un hook propio que escriba la URL por su cuenta.
- Cliente y temporizador se inyectan: los tests no tocan la red ni duermen.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
import yaml

from core.models import Market, Side, Signal
from ports.signal_source import SignalSource, signature_signal_id

BASE_URL = "https://api.helius.xyz"
ENV_VAR = "HELIUS_API_KEY"
CSV_HEADER = ("timestamp", "trader_id", "symbol", "side", "amount_usd", "signature")
DEFAULT_PAGE_LIMIT = 100  # máximo que admite Helius por página
DEFAULT_MAX_REQUESTS = 200
DEFAULT_PAUSE_SECONDS = 0.2
RETRIES_PER_PAGE = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0
TIMEOUT_SECONDS = 15.0

# Stablecoins por dirección de mint -> símbolo de la cotizada en el par.
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
WSOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_SYMBOLS = {WSOL_MINT: "SOL"}  # el mint de wSOL no es un símbolo que alguien quiera leer
LAMPORTS_PER_SOL_DECIMALS = 9
# Polvo de SOL: por debajo de 0,01 SOL una pata nativa/wSOL de la wallet NO es un token del swap,
# es el cierre de la cuenta wSOL, la devolución de alquiler o restos de comisión que Jupiter deja
# en casi todas las rutas. El alquiler de una cuenta de tokens son 0,00203928 SOL (2.039.280
# lamports), así que 0,01 SOL deja margen para varias cuentas cerradas a la vez (~5) y, a la vez,
# queda muy por debajo de cualquier swap real con SOL (a ~200 USD/SOL, 0,01 SOL son ~2 USD; una
# señal de menos que eso no se copiaría de todos modos). Un swap USDC<->SOL por debajo del umbral
# se omite como ``ambiguous``: sin patas que lo identifiquen, es mejor no emitirlo.
DUST_SOL = Decimal("0.01")

OMISSION_REASONS = {
    "no_stable": "swap sin stablecoin en ningún lado (token->token o SOL<->token): sin precio no se puede valorar",
    "stable_to_stable": "swap entre stablecoins: no es una posición que copiar",
    "ambiguous": "swap con varios tokens o varias stablecoins en un lado: no se sabe qué se compró",
    "zero_amount": "importe de la stablecoin igual a cero",
    "failed_tx": "transacción fallida en la cadena",
    "no_swap_event": "Helius no devolvió el evento de swap interpretado",
    "not_participant": "la wallet seguida no figura en las patas del swap",
}

_WALLET_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
_KEY_PARAM = re.compile(r"(?i)(api[-_]?key=)[^&\s'\"]+")


class HeliusError(Exception):
    """Fallo esperado al leer Helius; la CLI lo muestra como mensaje, nunca como traza."""


# --- la clave no se filtra ---

def _scrub(text: str, key: str = "") -> str:
    if key:
        text = text.replace(key, "***")
    return _KEY_PARAM.sub(r"\1***", text)


_SCRUBBED_ROOTS = ("httpx", "httpcore")


def _install_log_scrubbing() -> None:
    """Limpia ``api-key=...`` de TODO registro de httpx/httpcore y de sus hijos.

    Un ``logging.Filter`` de logger solo se aplica a los registros emitidos en ese mismo logger,
    no a los que llegan propagados desde hijos (``httpcore.http11``...). Por eso se envuelve la
    fábrica de registros: el texto se limpia al CREAR el registro, antes de que ningún logger,
    filtro o handler lo vea, sea cual sea el logger hijo y el handler que lo recoja.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, "_scrubs_api_key", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        try:
            if record.name.split(".")[0] in _SCRUBBED_ROOTS:
                message = record.getMessage()
                cleaned = _scrub(message)
                if cleaned != message:
                    record.msg = cleaned
                    record.args = None
        except Exception:  # noqa: BLE001 - limpiar nunca debe romper el logging
            pass
        return record

    factory._scrubs_api_key = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


_install_log_scrubbing()


# --- clave y wallets ---

def load_api_key(env: Mapping[str, str], root: Path) -> str:
    """``HELIUS_API_KEY`` del entorno o, si no está, del fichero ``.env`` de ``root``."""
    value = (env.get(ENV_VAR) or "").strip()
    if value:
        return value
    dotenv = Path(root) / ".env"
    try:
        text = dotenv.read_text(encoding="utf-8-sig")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, raw = line.partition("=")
        if sep and name.strip() == ENV_VAR:
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            if raw.strip():
                return raw.strip()
    raise HeliusError(
        f"falta {ENV_VAR}: defínela en el entorno o en {dotenv} (plantilla en config/.env.example)"
    )


def validate_wallet(wallet: str) -> str:
    if not isinstance(wallet, str) or not _WALLET_RE.match(wallet):
        raise HeliusError(f"dirección de wallet no válida (se esperan 32-44 caracteres base58): {wallet!r}")
    return wallet


def load_token_map(path: Path) -> dict[str, str]:
    """Mapeo opcional ``mint: SÍMBOLO`` (YAML o JSON) para los tokens que se siguen de cerca.

    Va en un fichero propio y no en el YAML del bot porque esa configuración es estricta
    (rechaza claves desconocidas) y pertenece al núcleo, que no sabe de Solana.
    """
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))  # JSON es YAML válido
    except (OSError, yaml.YAMLError) as exc:
        raise HeliusError(f"no se pudo leer el mapeo de tokens {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise HeliusError(f"el mapeo de tokens {path} debe ser un diccionario mint: símbolo")
    result: dict[str, str] = {}
    for mint, symbol in data.items():
        if not isinstance(mint, str) or not _WALLET_RE.match(mint):
            raise HeliusError(f"mapeo de tokens: {mint!r} no es una dirección de mint válida")
        clean = _sanitize_symbol(symbol)
        if clean is None:
            raise HeliusError(
                f"mapeo de tokens: símbolo no válido para {mint}: {symbol!r} (letras, cifras, . _ -; máx. 20)"
            )
        result[mint] = clean
    return result


# --- interpretación de una transacción ---

def _fail(sig: str | None, message: str) -> HeliusError:
    return HeliusError(f"transacción {sig}: {message}" if sig else message)


def _header(tx: Any) -> tuple[str, datetime]:
    if not isinstance(tx, dict):
        raise HeliusError(f"se esperaba un objeto por transacción y llegó {type(tx).__name__}")
    sig = tx.get("signature")
    if not isinstance(sig, str) or not sig.strip():
        raise HeliusError("transacción sin firma ('signature')")
    ts = tx.get("timestamp")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise _fail(sig, f"'timestamp' no es un número de segundos unix: {ts!r}")
    try:
        return sig, datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise _fail(sig, f"'timestamp' fuera de rango: {ts!r}") from None


def _legs(sig: str, event: dict, name: str) -> list[dict]:
    raw = event.get(name)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _fail(sig, f"'{name}' debería ser una lista y es {type(raw).__name__}")
    return raw


def _amount(sig: str, leg: Any) -> tuple[str, Decimal]:
    """(mint, importe en unidades de token) de una pata; error si la pata no es legible."""
    if not isinstance(leg, dict):
        raise _fail(sig, f"pata de swap ilegible: {type(leg).__name__}")
    mint = leg.get("mint")
    if not isinstance(mint, str) or not mint:
        raise _fail(sig, "pata de swap sin 'mint'")
    raw = leg.get("rawTokenAmount")
    if not isinstance(raw, dict):
        raise _fail(sig, f"pata {mint} sin 'rawTokenAmount'")
    decimals = raw.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 30:
        raise _fail(sig, f"pata {mint}: 'decimals' no es un entero válido: {decimals!r}")
    return mint, _units(sig, f"pata {mint}: 'tokenAmount'", raw.get("tokenAmount")).scaleb(-decimals)


def _units(sig: str, what: str, value: Any) -> Decimal:
    """Unidades mínimas: un entero sin signo. Cualquier otra forma delata unidades de interfaz."""
    if value is None or isinstance(value, bool):
        raise _fail(sig, f"{what} ausente o inválido")
    text = str(value)  # nunca Decimal(float)
    if not re.fullmatch(r"[0-9]+", text):
        raise _fail(
            sig,
            f"{what} no es un entero de unidades mínimas ({value!r}); ¿manda Helius unidades de "
            "interfaz? Con ellas el importe saldría mal por un factor 10^decimals, así que se aborta",
        )
    return Decimal(text)


def _sanitize_symbol(value: Any) -> str | None:
    if isinstance(value, str) and _SYMBOL_RE.match(value.strip()):
        return value.strip()
    return None


def _wallet_side(
    sig: str, legs: list, native: Any, wallet: str, token_symbols: dict[str, str]
) -> dict[str, Decimal]:
    """Importe por mint de un lado del swap, solo con las patas de la wallet seguida."""
    totals: dict[str, Decimal] = {}
    for leg in legs:
        mint, amount = _amount(sig, leg)  # se valida todo, también lo ajeno
        if leg.get("userAccount") != wallet:
            continue
        totals[mint] = totals.get(mint, Decimal(0)) + amount
        symbol = _sanitize_symbol(leg.get("symbol"))
        if symbol:
            token_symbols.setdefault(mint, symbol)
    if native is not None:
        if not isinstance(native, dict):
            raise _fail(sig, f"patas nativas ilegibles: {type(native).__name__}")
        if native.get("account") == wallet:
            lamports = _units(sig, "nativo 'amount'", native.get("amount"))
            totals[WSOL_MINT] = totals.get(WSOL_MINT, Decimal(0)) + lamports.scaleb(-LAMPORTS_PER_SOL_DECIMALS)
    if WSOL_MINT in totals and totals[WSOL_MINT] < DUST_SOL:
        del totals[WSOL_MINT]  # polvo: no es un token del swap
    return totals


def make_signal_id(signature: str, wallet: str) -> str:
    """Id estable de la señal: solo depende de la firma y de la wallet seguida."""
    return signature_signal_id(signature, wallet)


def parse_swap(
    tx: Any, wallet: str, token_map: Mapping[str, str] | None = None
) -> Signal | str:
    """Una transacción de Helius -> ``Signal``, o el código de por qué se omite.

    Lanza ``HeliusError`` si la forma de la transacción es inesperada.
    """
    sig, moment = _header(tx)
    if tx.get("transactionError"):
        return "failed_tx"
    events = tx.get("events")
    if events is None:
        return "no_swap_event"
    if not isinstance(events, dict):
        raise _fail(sig, f"'events' debería ser un objeto y es {type(events).__name__}")
    event = events.get("swap")
    if event is None or event == {}:
        return "no_swap_event"
    if not isinstance(event, dict):
        raise _fail(sig, f"'events.swap' debería ser un objeto y es {type(event).__name__}")

    symbols: dict[str, str] = {}
    sold = _wallet_side(sig, _legs(sig, event, "tokenInputs"), event.get("nativeInput"), wallet, symbols)
    got = _wallet_side(sig, _legs(sig, event, "tokenOutputs"), event.get("nativeOutput"), wallet, symbols)
    if not sold and not got:
        return "not_participant"

    stable_sold = [m for m in sold if m in STABLE_MINTS]
    stable_got = [m for m in got if m in STABLE_MINTS]
    token_sold = [m for m in sold if m not in STABLE_MINTS]
    token_got = [m for m in got if m not in STABLE_MINTS]
    if not stable_sold and not stable_got:
        return "no_stable"
    if not token_sold and not token_got:
        return "stable_to_stable" if stable_sold and stable_got else "ambiguous"

    if len(stable_sold) == 1 and not token_sold and not stable_got and len(token_got) == 1:
        side, stable, token, amounts = Side.BUY, stable_sold[0], token_got[0], sold
    elif len(stable_got) == 1 and not token_got and not stable_sold and len(token_sold) == 1:
        side, stable, token, amounts = Side.SELL, stable_got[0], token_sold[0], got
    else:
        return "ambiguous"

    usd = amounts[stable]
    if usd <= 0:
        return "zero_amount"
    base = (token_map or {}).get(token) or DEFAULT_SYMBOLS.get(token) or symbols.get(token) or token
    return Signal(
        id=make_signal_id(sig, wallet),
        trader_id=wallet,
        market=Market.SOLANA,
        symbol=f"{base}/{STABLE_MINTS[stable]}",
        side=side,
        timestamp=moment,
        trader_amount_usd=usd,
    )


# --- descarga ---

@dataclass
class SwapScan:
    signals: list[Signal] = field(default_factory=list)
    omitted: dict[str, int] = field(default_factory=dict)
    signatures: dict[str, str] = field(default_factory=dict)  # id de la señal -> firma
    requests: int = 0
    stop: str = ""  # empty | no_advance | since | max_requests

    @property
    def complete(self) -> bool:
        return self.stop != "max_requests"


def make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _request_page(
    client: Any,
    wallet: str,
    api_key: str,
    before: str | None,
    limit: int,
    sleep: Callable[[float], None],
    counter: list[int],
    max_requests: int,
) -> list:
    """Una página, con reintento acotado ante 429, 5xx y fallos de red. Cada intento cuenta."""
    params = {"api-key": api_key, "type": "SWAP", "limit": str(limit)}
    if before is not None:
        params["before"] = before
    url = f"{BASE_URL}/v0/addresses/{wallet}/transactions"
    last = "sin detalle"
    for attempt in range(RETRIES_PER_PAGE):
        if counter[0] >= max_requests:
            raise HeliusError(f"tope de {max_requests} peticiones agotado reintentando (último fallo: {last})")
        counter[0] += 1
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:  # red, timeouts: transitorios
            last = f"{type(exc).__name__}: {_scrub(str(exc), api_key)[:120]}"
        except Exception as exc:  # noqa: BLE001
            raise HeliusError(f"error inesperado al llamar a Helius: {type(exc).__name__}") from None
        else:
            status = response.status_code
            if status in (401, 403):
                raise HeliusError(f"Helius rechazó la clave de API (HTTP {status}): revisa {ENV_VAR}")
            if status == 429 or status >= 500:
                last = f"HTTP {status}"
            elif status != 200:
                raise HeliusError(f"Helius respondió HTTP {status}")
            else:
                return _decode(response, api_key)
        if attempt < RETRIES_PER_PAGE - 1:
            sleep(min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS))
    raise HeliusError(f"{RETRIES_PER_PAGE} intentos fallidos contra Helius (último fallo: {last})")


def _decode(response: httpx.Response, api_key: str) -> list:
    try:
        body = response.json()
    except ValueError:
        raise HeliusError("la respuesta de Helius no es JSON") from None
    if not isinstance(body, list):
        detail = ""
        if isinstance(body, dict) and "error" in body:
            detail = f" (error: {_scrub(str(body['error']), api_key)[:120]})"
        raise HeliusError(
            f"respuesta inesperada de Helius: se esperaba una lista de transacciones y llegó "
            f"{type(body).__name__}{detail}"
        )
    return body


def fetch_wallet_swaps(
    client: Any,
    wallet: str,
    api_key: str,
    *,
    since: datetime | None = None,
    token_map: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    pause: float = DEFAULT_PAUSE_SECONDS,
) -> SwapScan:
    """Swaps de una wallet con ``timestamp >= since`` (todo el historial accesible si es None)."""
    validate_wallet(wallet)
    scan = SwapScan(stop="max_requests")
    counter = [0]
    seen: set[str] = set()
    before: str | None = None
    newest_prev: datetime | None = None  # hora de la última transacción de la página anterior

    while counter[0] < max_requests:
        if counter[0] > 0:
            sleep(pause)
        page = _request_page(client, wallet, api_key, before, page_limit, sleep, counter, max_requests)
        if not page:
            scan.stop = "empty"
            break
        headers = [_header(tx) for tx in page]
        stamps = ([newest_prev] if newest_prev is not None else []) + [h[1] for h in headers]
        if any(later > earlier for earlier, later in zip(stamps, stamps[1:])):
            raise HeliusError(
                "las transacciones no vienen de más nueva a más antigua: el orden de Helius cambió y "
                "--since/la paginación darían un resultado truncado; se aborta"
            )
        newest_prev = headers[-1][1]
        fresh = [(tx, h) for tx, h in zip(page, headers) if h[0] not in seen]
        if not fresh:
            scan.stop = "no_advance"
            break
        for tx, (sig, moment) in fresh:
            seen.add(sig)
            if since is not None and moment < since:
                continue
            result = parse_swap(tx, wallet, token_map)
            if isinstance(result, str):
                scan.omitted[result] = scan.omitted.get(result, 0) + 1
            else:
                scan.signals.append(result)
                scan.signatures[result.id] = sig
        if since is not None and min(h[1] for h in headers) < since:
            scan.stop = "since"
            break
        before = headers[-1][0]
    scan.requests = counter[0]
    return scan


class SolanaWalletSignalSource(SignalSource):
    """Señales a partir de los swaps de unas wallets de Solana (Helius)."""

    name = "solana"

    def __init__(
        self,
        wallets: Iterable[str],
        api_key: str,
        *,
        client: Any = None,
        token_map: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ) -> None:
        self._wallets = [validate_wallet(w) for w in dict.fromkeys(wallets)]
        self._api_key = api_key
        self._client = client if client is not None else make_client()
        self._token_map = dict(token_map or {})
        self._sleep = sleep
        self._page_limit = page_limit
        self._max_requests = max_requests
        self.last_omitted: dict[str, int] = {}
        self.last_complete = True
        self.last_signatures: dict[str, str] = {}

    def fetch_trades(self, since: datetime | None) -> list[Signal]:
        signals: list[Signal] = []
        self.last_omitted = {}
        self.last_complete = True
        self.last_signatures = {}
        for wallet in self._wallets:
            scan = fetch_wallet_swaps(
                self._client, wallet, self._api_key, since=since, token_map=self._token_map,
                sleep=self._sleep, page_limit=self._page_limit, max_requests=self._max_requests,
            )
            signals.extend(scan.signals)
            self.last_signatures.update(scan.signatures)
            self.last_complete = self.last_complete and scan.complete
            for reason, n in scan.omitted.items():
                self.last_omitted[reason] = self.last_omitted.get(reason, 0) + n
        if since is not None:
            signals = [s for s in signals if s.timestamp > since]
        return sorted(signals, key=lambda s: (s.timestamp, s.id))

    def list_followed(self) -> list[str]:
        return sorted(self._wallets)


def write_signals_csv(
    path: Path, signals: list[Signal], signatures: Mapping[str, str] | None = None
) -> None:
    """Escribe ``timestamp,trader_id,symbol,side,amount_usd,signature`` de forma atómica.

    ``signatures`` (id de señal -> firma) rellena la sexta columna; el lector de CSV del backtest
    deriva de ella el id, que así coincide con el de la señal en vivo.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for s in signals:
                amount = s.trader_amount_usd
                writer.writerow([
                    s.timestamp.astimezone(timezone.utc).isoformat(), s.trader_id, s.symbol, s.side.value,
                    "" if amount is None else format(amount.normalize(), "f"),
                    (signatures or {}).get(s.id, ""),
                ])
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HeliusError(f"no se pudo escribir {path}: {exc}") from exc
