"""Descarga de cotizaciones históricas (velas) de un exchange vía ccxt.

Escribe el CSV ``timestamp,symbol,price`` que lee el backtest. Decisiones que no son obvias:

- **Sello = cierre de la vela, no apertura.** La vela de las 09:00 (1h) no se conoce hasta las
  10:00. El backtest usa la última cotización con sello <= al reloj simulado: sellar con la
  apertura le entregaría a las 09:00 un precio de las 10:00 (lookahead) y el backtest saldría
  mejor de lo que el bot habría podido operar.
- **Se descarta la vela aún sin cerrar**: su "cierre" es el precio de ahora mismo y cambiaría
  si se repitiera la descarga.
- **Paginación**: se pide desde ``since`` y se avanza a ``última vela + un intervalo``. Se
  para si la página viene vacía, si la última vela no avanza (un exchange que ignora
  ``since`` devolvería siempre lo mismo) o si se supera ``until``. Hay además un tope duro de
  peticiones (los reintentos también cuentan) para que un error de cálculo no pida
  eternamente. Si el tope corta la descarga, el resultado lo dice (``complete=False``): un
  histórico recortado en silencio daría un backtest engañoso.
- El cliente y el temporizador se inyectan: los tests no tocan la red ni duermen.
"""

from __future__ import annotations

import csv
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import ccxt

from core.models import Quote

CSV_HEADER = ("timestamp", "symbol", "price")
DEFAULT_PAGE_LIMIT = 1000
DEFAULT_MAX_REQUESTS = 2000
RETRIES_PER_PAGE = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0
FALLBACK_RATE_LIMIT_MS = 1000

_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_TIMEFRAME_RE = re.compile(r"^([1-9][0-9]*)([smhdw])$")


class HistoryError(Exception):
    """Fallo esperado de la descarga; la CLI lo muestra como mensaje, nunca como traza."""


class SymbolNotFound(HistoryError):
    """El símbolo no existe en el exchange."""


@dataclass
class HistoryResult:
    quotes: list[Quote] = field(default_factory=list)
    requests: int = 0
    stop: str = ""  # empty | no_advance | until | max_requests
    skipped: int = 0  # filas ilegibles o con cierre inutilizable

    @property
    def complete(self) -> bool:
        return self.stop != "max_requests"


def interval_ms(timeframe: str) -> int:
    """Milisegundos de una temporalidad. Meses y años se rechazan: no tienen longitud fija,
    y avanzar "un intervalo" desde una vela de enero caería dentro de la misma vela."""
    match = _TIMEFRAME_RE.match(timeframe or "")
    if match is None:
        raise HistoryError(
            f"temporalidad no soportada: {timeframe!r} (se admiten s, m, h, d, w; p. ej. 15m, 1h, 1d)"
        )
    return int(match.group(1)) * _UNIT_MS[match.group(2)]


def make_client(exchange_id: str) -> Any:
    """Cliente ccxt sin throttling propio: la espera la hacemos nosotros (y es inyectable)."""
    exchange_cls = getattr(ccxt, exchange_id, None) if exchange_id in ccxt.exchanges else None
    if exchange_cls is None:
        raise HistoryError(f"exchange desconocido: {exchange_id!r}")
    return exchange_cls({"enableRateLimit": False})


def validate_timeframe(client: Any, timeframe: str) -> None:
    interval_ms(timeframe)
    has = getattr(client, "has", None) or {}
    if not has.get("fetchOHLCV"):
        raise HistoryError("el exchange no ofrece velas históricas (fetchOHLCV)")
    supported = getattr(client, "timeframes", None) or {}
    if timeframe not in supported:
        listed = ", ".join(supported) if supported else "ninguna declarada"
        raise HistoryError(f"el exchange no soporta la temporalidad {timeframe!r} (disponibles: {listed})")


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _request_page(
    client: Any,
    symbol: str,
    timeframe: str,
    since_ms: int,
    limit: int,
    sleep: Callable[[float], None],
    counter: list[int],
    max_requests: int,
) -> list:
    """Una página, con reintento acotado ante límite de tasa y fallos de red.
    Cada intento cuenta contra el tope duro."""
    last_error: Exception | None = None
    for attempt in range(RETRIES_PER_PAGE):
        if counter[0] >= max_requests:
            raise HistoryError(
                f"tope de {max_requests} peticiones agotado reintentando {symbol}"
                + (f" (último error: {last_error})" if last_error else "")
            )
        counter[0] += 1
        try:
            page = client.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        except ccxt.BadSymbol as exc:
            raise SymbolNotFound(f"el símbolo {symbol} no existe en el exchange: {exc}") from exc
        except ccxt.NetworkError as exc:  # incluye RateLimitExceeded
            last_error = exc
            sleep(min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS))
            continue
        except ccxt.BaseError as exc:
            raise HistoryError(f"error del exchange en {symbol}: {exc}") from exc
        except Exception as exc:  # parseo interno de ccxt, sockets...
            raise HistoryError(f"error inesperado en {symbol}: {exc!r}") from exc
        if not isinstance(page, list):
            raise HistoryError(f"respuesta de velas ilegible para {symbol}: {type(page).__name__}")
        return page
    raise HistoryError(f"{symbol}: {RETRIES_PER_PAGE} intentos fallidos (último error: {last_error})")


def _close_of(row: Any) -> tuple[int, Decimal | None] | None:
    """(apertura_ms, cierre) de una fila ``[ts, o, h, l, c, v]``; None si no tiene ni marca temporal.
    El cierre es None si no es utilizable (ausente, no numérico, no finito o no positivo)."""
    if not isinstance(row, (list, tuple)) or len(row) < 5:
        return None
    opened = row[0]
    if isinstance(opened, bool) or not isinstance(opened, (int, float)):
        return None
    raw = row[4]
    if raw is None or isinstance(raw, bool):
        return int(opened), None
    try:
        price = Decimal(str(raw))  # nunca Decimal(float)
    except InvalidOperation:
        return int(opened), None
    if not price.is_finite() or price <= 0:
        return int(opened), None
    return int(opened), price


def fetch_candles(
    client: Any,
    symbol: str,
    timeframe: str,
    since: datetime,
    until: datetime,
    *,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> HistoryResult:
    """Cotizaciones de cierre de las velas con apertura en [since, until).

    Cada ``Quote`` lleva como marca temporal el CIERRE de su vela (apertura + intervalo).
    """
    step = interval_ms(timeframe)
    markets = getattr(client, "markets", None)
    if markets and symbol not in markets:
        raise SymbolNotFound(f"el símbolo {symbol} no existe en el exchange")
    rate_limit = getattr(client, "rateLimit", None)
    pause = (rate_limit if isinstance(rate_limit, (int, float)) and rate_limit > 0 else FALLBACK_RATE_LIMIT_MS) / 1000

    since_ms, until_ms, now_ms = _ms(since), _ms(until), _ms(now)
    closes: dict[int, Decimal] = {}  # apertura_ms -> cierre; la clave deduplica páginas solapadas
    result = HistoryResult()
    counter = [0]
    cursor = since_ms
    last_seen: int | None = None
    result.stop = "max_requests"

    while counter[0] < max_requests:
        if counter[0] > 0:
            sleep(pause)
        page = _request_page(client, symbol, timeframe, cursor, page_limit, sleep, counter, max_requests)
        if not page:
            result.stop = "empty"
            break
        newest: int | None = None
        for row in page:
            parsed = _close_of(row)
            if parsed is None:
                result.skipped += 1
                continue
            opened, price = parsed
            newest = opened if newest is None else max(newest, opened)
            if opened < since_ms or opened >= until_ms:
                continue
            if price is None:
                result.skipped += 1
                continue
            closes[opened] = price
        if newest is None or (last_seen is not None and newest <= last_seen):
            result.stop = "no_advance"
            break
        if newest >= until_ms:
            result.stop = "until"
            break
        last_seen = newest
        cursor = newest + step

    result.requests = counter[0]
    result.quotes = [
        Quote(symbol, price, _from_ms(opened + step))
        for opened, price in sorted(closes.items())
        if opened + step <= now_ms  # vela aún sin cerrar fuera
    ]
    return result


def write_quotes_csv(path: Path, quotes: list[Quote]) -> None:
    """Escribe ``timestamp,symbol,price`` de forma atómica: un fallo no deja medio fichero."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for q in quotes:
                writer.writerow(
                    [q.timestamp.astimezone(timezone.utc).isoformat(), q.symbol, format(q.price.normalize(), "f")]
                )
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HistoryError(f"no se pudo escribir {path}: {exc}") from exc
