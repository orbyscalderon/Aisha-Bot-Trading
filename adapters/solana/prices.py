"""Precios de tokens de Solana: histórico (GeckoTerminal) y actual (Jupiter).

POR QUÉ EXISTE. ``fetch-quotes`` saca precios de Binance, donde BONK y WIF no cotizan. Sin
estos precios, las señales reales de Solana no se pueden backtestear ni el motor puede
dimensionar o cerrar una posición: sabe que compró BONK, pero no cuánto vale.

Dos fuentes porque hacen falta dos cosas distintas:

- **Histórico** (``fetch_token_quotes``, GeckoTerminal): velas OHLCV por pool. Gratis y sin clave.
- **Actual** (``JupiterPriceSource``): precio en dólares ahora mismo, para el bot en marcha.

Trampas de las APIs reales, comprobadas contra ellas y vigiladas por tests:

- **GeckoTerminal sella las velas por APERTURA**; todo este proyecto las sella por CIERRE. La
  vela abierta a las 10:00 no se conoce hasta las 11:00. Sellarla a las 10:00 daría por sabido
  un precio una hora antes de existir, que es la forma clásica de que un backtest mienta a su
  favor. Se suma un intervalo, igual que en ``adapters/cex/history.py``.
- **La primera vela que devuelve es la hora EN CURSO.** Su "cierre" es el precio de este
  instante y cambiaría en la siguiente consulta. Se descarta.
- **Los números llegan como float de JSON.** Siempre ``Decimal(str(x))``: BONK vale
  0,0000027 USD y pasar por float perdería cifras justo donde están todas.
- **Se elige el pool con MÁS liquidez.** El precio de un pool pequeño se mueve con poco dinero,
  así que es manipulable; y GeckoTerminal no garantiza el orden de la lista.

Distinción que importa en los errores del precio actual: un token que Jupiter no cotiza es
``MarketUnpriceable`` (permanente: quien suma el capital total puede omitirlo), mientras que un
corte de red es ``QuoteUnavailable`` a secas (transitorio: debe propagarse, o se dimensionaría
contra una cifra equivocada).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

import httpx

from core.models import Quote
from ports.market import MarketDataSource, MarketUnpriceable, QuoteUnavailable

logger = logging.getLogger(__name__)

GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
JUPITER_URL = "https://lite-api.jup.ag/price/v3"
TIMEFRAME_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}
PAGE_LIMIT = 1000           # el máximo que admite GeckoTerminal
MAX_PAGES = 40              # tope duro: 40.000 velas horarias son más de 4 años
PAUSE_SECONDS = 2.5         # el plan gratuito admite ~30 peticiones/minuto
RETRIES = 4                 # ante 429 y 5xx; el limite de tasa es lo normal, no lo raro
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 40.0
TIMEOUT_SECONDS = 25.0
JUPITER_BATCH = 50          # ids por petición
DEFAULT_CACHE_SECONDS = 20
HEADERS = {"Accept": "application/json", "User-Agent": "bot-copy-trading/1.0"}


class SolanaPriceError(Exception):
    """Fallo esperado bajando precios; la CLI lo muestra como mensaje, nunca como traza."""


def _decimal(value: Any) -> Decimal | None:
    """Decimal exacto desde JSON, o None si el valor no sirve como precio."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        price = Decimal(str(value))  # str() SIEMPRE: nunca Decimal(float)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price


# --- histórico ---

def pick_pool(pools: Iterable[Any]) -> tuple[str, Decimal]:
    """``(id del pool, liquidez)`` del pool con más liquidez. El precio de uno pequeño se
    mueve con poco dinero, así que tomarlo por referencia es tomar un precio manipulable."""
    mejor_id: str | None = None
    mejor_liq = Decimal(-1)
    for p in pools or []:
        if not isinstance(p, dict):
            continue
        crudo = str(p.get("id") or "")
        pid = crudo.split("_", 1)[1] if "_" in crudo else crudo
        if not pid:
            continue
        liq = _decimal((p.get("attributes") or {}).get("reserve_in_usd")) or Decimal(0)
        if mejor_id is None or liq > mejor_liq:
            mejor_id, mejor_liq = pid, liq
    if mejor_id is None:
        raise SolanaPriceError(
            "sin pools para este token en GeckoTerminal: no hay de dónde sacar su histórico"
        )
    return mejor_id, mejor_liq


def _get(
    client: Any,
    url: str,
    params: dict | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = RETRIES,
) -> Any:
    """Una peticion a GeckoTerminal, con reintento ante limite de tasa y fallos pasajeros.

    El 429 NO es un error del que rendirse: el plan gratuito limita a unas 30 peticiones por
    minuto y bajar varios tokens lo roza con normalidad. Sin reintento, media descarga se
    perderia por algo que se arregla esperando unos segundos. Un 404 o un 400, en cambio, no
    mejoran esperando: se cortan a la primera.
    """
    ultimo = "sin detalle"
    for intento in range(retries):
        try:
            response = client.get(url, params=params, headers=HEADERS)
        except Exception as exc:  # noqa: BLE001 - red y timeouts son pasajeros
            ultimo = f"{type(exc).__name__}"
        else:
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise SolanaPriceError("la respuesta de GeckoTerminal no es JSON") from None
            if status != 429 and status < 500:
                raise SolanaPriceError(f"GeckoTerminal respondió HTTP {status}")
            ultimo = f"HTTP {status}"
        if intento < retries - 1:
            sleep(min(BACKOFF_BASE_SECONDS * 2**intento, BACKOFF_CAP_SECONDS))
    raise SolanaPriceError(f"GeckoTerminal no respondió tras {retries} intentos ({ultimo})")


def fetch_token_quotes(
    client: Any,
    mint: str,
    symbol: str,
    *,
    since: datetime,
    until: datetime | None = None,
    timeframe: str = "hour",
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_pages: int = MAX_PAGES,
) -> list[Quote]:
    """Cotizaciones de CIERRE del token, de la más antigua a la más nueva.

    ``symbol`` es el que llevarán las ``Quote``: tiene que ser el MISMO que el de las señales
    (``BONK/USDC``), o el backtest no emparejaría ninguna.
    """
    step = TIMEFRAME_SECONDS.get(timeframe)
    if step is None:
        admitidas = ", ".join(sorted(TIMEFRAME_SECONDS))
        raise SolanaPriceError(f"temporalidad no admitida: {timeframe!r} (admitidas: {admitidas})")

    ahora = now or datetime.now(timezone.utc)
    limite_superior = min(until, ahora) if until is not None else ahora
    desde_s = int(since.timestamp())

    pools = _get(client, f"{GECKO_BASE}/tokens/{mint}/pools", {"page": 1}, sleep)
    pool_id, liquidez = pick_pool(pools.get("data") if isinstance(pools, dict) else None)
    logger.info("pool elegido para %s: %s (liquidez %s USD)", symbol, pool_id, liquidez)

    cierres: dict[int, Decimal] = {}  # apertura -> cierre; la clave deduplica páginas solapadas
    antes: int | None = None
    for pagina in range(max_pages):
        if pagina:
            sleep(PAUSE_SECONDS)
        params: dict[str, Any] = {"aggregate": 1, "limit": PAGE_LIMIT, "currency": "usd"}
        if antes is not None:
            params["before_timestamp"] = antes
        cuerpo = _get(client, f"{GECKO_BASE}/pools/{pool_id}/ohlcv/{timeframe}", params, sleep)
        # Paso a paso y con isinstance: una forma inesperada se salta, no revienta con un
        # AttributeError a mitad de la descarga.
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else None
        atributos = datos.get("attributes") if isinstance(datos, dict) else None
        filas = atributos.get("ohlcv_list") if isinstance(atributos, dict) else None
        if not isinstance(filas, list) or not filas:
            break

        mas_antigua: int | None = None
        for fila in filas:
            if not isinstance(fila, (list, tuple)) or len(fila) < 5:
                continue
            apertura = fila[0]
            if isinstance(apertura, bool) or not isinstance(apertura, (int, float)):
                continue
            apertura = int(apertura)
            mas_antigua = apertura if mas_antigua is None else min(mas_antigua, apertura)
            precio = _decimal(fila[4])
            if precio is not None:
                cierres[apertura] = precio

        if mas_antigua is None or mas_antigua <= desde_s:
            break
        antes = mas_antigua  # la API devuelve lo estrictamente anterior a este sello

    tope = int(limite_superior.timestamp())
    quotes = [
        Quote(symbol, precio, datetime.fromtimestamp(apertura + step, tz=timezone.utc))
        for apertura, precio in sorted(cierres.items())
        # El cierre tiene que haber ocurrido ya (vela en curso fuera) y caer dentro de [since, until].
        if desde_s <= apertura and apertura + step <= tope
    ]
    return quotes


# --- precio actual ---

class JupiterPriceSource(MarketDataSource):
    """Precio en dólares ahora mismo, por mint, vía Jupiter.

    Cachea unos segundos y pide todos los símbolos conocidos de una vez: un ciclo de salidas
    consulta todas las posiciones abiertas, y sin caché serían N peticiones por ciclo.
    """

    def __init__(
        self,
        symbol_to_mint: Mapping[str, str],
        *,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
    ) -> None:
        self._mints = dict(symbol_to_mint)
        self._client = client if client is not None else httpx.Client(timeout=TIMEOUT_SECONDS)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache_seconds = cache_seconds
        self._cache: dict[str, Decimal] = {}
        self._cached_at: datetime | None = None

    def get_quote(self, symbol: str) -> Quote:
        mint = self._mints.get(symbol)
        if mint is None:
            # Permanente: sin mint no se podrá valorar nunca, por muchas veces que se reintente.
            raise MarketUnpriceable(f"{symbol}: no está en el mapa de mints de Solana")

        ahora = self._now()
        if self._cached_at is None or (ahora - self._cached_at).total_seconds() >= self._cache_seconds:
            self._refresh(ahora)
        precio = self._cache.get(mint)
        if precio is None:
            raise MarketUnpriceable(f"{symbol}: Jupiter no da precio para {mint}")
        return Quote(symbol, precio, ahora)

    def _refresh(self, ahora: datetime) -> None:
        mints = list(dict.fromkeys(self._mints.values()))
        nuevos: dict[str, Decimal] = {}
        for i in range(0, len(mints), JUPITER_BATCH):
            lote = mints[i:i + JUPITER_BATCH]
            try:
                response = self._client.get(
                    JUPITER_URL, params={"ids": ",".join(lote)}, headers=HEADERS
                )
                if response.status_code != 200:
                    # Transitorio: NO es MarketUnpriceable. Confundirlos haría que el capital
                    # total se calculase omitiendo posiciones que sí valen algo.
                    raise QuoteUnavailable(f"Jupiter respondió HTTP {response.status_code}")
                cuerpo = response.json()
            except QuoteUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                raise QuoteUnavailable(f"fallo consultando Jupiter: {type(exc).__name__}") from None
            if not isinstance(cuerpo, dict):
                raise QuoteUnavailable("respuesta inesperada de Jupiter")
            for mint, datos in cuerpo.items():
                precio = _decimal(datos.get("usdPrice")) if isinstance(datos, dict) else None
                if precio is not None:
                    nuevos[mint] = precio
        self._cache = nuevos
        self._cached_at = ahora

    def get_balance(self) -> Decimal:
        raise NotImplementedError(
            "JupiterPriceSource solo lee precios. El capital lo aporta el envoltorio: "
            "PaperCapitalSource en simulado, y el adaptador de la wallet en real."
        )
