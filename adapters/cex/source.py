"""Fuente de precios (ccxt) y de señales (CSV) para exchanges centralizados."""

from __future__ import annotations

import csv
import hashlib
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

import ccxt

from core.models import Market, Quote, Side, Signal
from ports.market import MarketDataSource, QuoteUnavailable
from ports.signal_source import SignalSource

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0

# Activos que valen 1 USD; el resto se valora con su par contra USDT.
_STABLES = frozenset({"USDT", "USDC", "USD", "BUSD", "FDUSD"})
# Pares contra los que se intenta valorar un activo, en este orden.
_QUOTE_ASSETS = ("USDT", "USDC", "FDUSD", "BUSD")

logger = logging.getLogger(__name__)


class _MarketMissing(QuoteUnavailable):
    """El par no existe en el exchange (no es un fallo transitorio)."""


class _NoPrice(QuoteUnavailable):
    """El par existe pero su ticker no trae precio utilizable (nulo o no positivo)."""


def _to_decimal(value: Any, what: str) -> Decimal:
    """Convierte con Decimal(str(valor)): nunca Decimal(float)."""
    if value is None or isinstance(value, bool):
        raise QuoteUnavailable(f"{what}: valor ausente o inválido")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise QuoteUnavailable(f"{what}: valor no numérico {value!r}") from exc
    if not result.is_finite():
        raise QuoteUnavailable(f"{what}: valor no finito {value!r}")
    return result


class CexDataSource(MarketDataSource):
    def __init__(
        self,
        exchange_id: str,
        api_key: str | None = None,
        secret: str | None = None,
        testnet: bool = False,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        ignore_assets: Iterable[str] = (),
    ) -> None:
        exchange_cls = getattr(ccxt, exchange_id, None)
        if exchange_id not in ccxt.exchanges or exchange_cls is None:
            raise ValueError(f"exchange desconocido: {exchange_id}")
        config: dict[str, Any] = {"enableRateLimit": True}
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret
        client = exchange_cls(config)
        if testnet:
            client.set_sandbox_mode(True)
        self._init(client, sleep, max_attempts, ignore_assets)

    def _init(
        self,
        client: Any,
        sleep: Callable[[float], None],
        max_attempts: int,
        ignore_assets: Iterable[str] = (),
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._ignore_assets = frozenset(ignore_assets)
        self._warned_assets: set[str] = set()

    @classmethod
    def from_client(
        cls,
        client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        ignore_assets: Iterable[str] = (),
    ) -> "CexDataSource":
        """Construye la fuente con un cliente ya hecho (los tests no tocan la red)."""
        self = cls.__new__(cls)
        self._init(client, sleep, max_attempts, ignore_assets)
        return self

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Llama a ccxt. Solo reintenta RateLimitExceeded; todo error sale como QuoteUnavailable."""
        for attempt in range(self._max_attempts):
            try:
                return fn(*args)
            except ccxt.RateLimitExceeded as exc:
                if attempt == self._max_attempts - 1:
                    raise QuoteUnavailable(f"límite de tasa agotado: {exc}") from exc
                self._sleep(min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS))
            except ccxt.BadSymbol as exc:
                raise _MarketMissing(f"el mercado no existe: {exc}") from exc
            except ccxt.BaseError as exc:
                raise QuoteUnavailable(f"error de ccxt: {exc}") from exc
            except Exception as exc:  # parseo interno de ccxt, sockets...
                raise QuoteUnavailable(f"error inesperado: {exc!r}") from exc
        raise QuoteUnavailable("sin intentos disponibles")  # max_attempts <= 0

    def _last_price(self, symbol: str) -> Decimal:
        ticker = self._call(self._client.fetch_ticker, symbol)
        if not isinstance(ticker, dict):
            # Respuesta ilegible (no es "este mercado no tiene precio"): transitoria.
            raise QuoteUnavailable(f"respuesta de ticker ilegible para {symbol}: {ticker!r}")
        raw = ticker.get("last")
        if raw is None:
            raise _NoPrice(f"sin precio para {symbol}")
        price = _to_decimal(raw, f"precio de {symbol}")
        if price <= 0:
            raise _NoPrice(f"precio no positivo para {symbol}: {price}")
        return price

    def get_quote(self, symbol: str) -> Quote:
        price = self._last_price(symbol)
        return Quote(symbol=symbol, price=price, timestamp=datetime.now(timezone.utc))

    def get_balance(self) -> Decimal:
        """Capital TOTAL de la cuenta en USD, incluido el valor de las posiciones abiertas.

        Usa el campo `total` de ccxt, NO `free`: con efectivo libre, abrir una
        posición bajaría esta cifra por debajo del pico persistido y la
        protección de drawdown bloquearía todas las entradas para siempre.
        Los activos no estables se valoran a su último precio contra USDT; si
        alguno falla de forma transitoria se lanza QuoteUnavailable en vez de
        infravalorar el capital (ver _asset_price para qué se omite).

        `ignore_assets` se comprueba ANTES de la rama de stablecoins: listar
        "USDT" ahí quita el efectivo del capital. Probablemente es lo que se
        quiere, pero conviene saberlo.
        """
        balance = self._call(self._client.fetch_balance)
        totals = balance.get("total") if isinstance(balance, dict) else None
        if not isinstance(totals, dict):
            raise QuoteUnavailable("balance sin campo 'total'")
        capital = Decimal(0)
        for asset, amount in totals.items():
            if asset in self._ignore_assets:
                continue
            qty = Decimal(0) if amount is None else _to_decimal(amount, f"saldo de {asset}")
            if qty == 0:
                continue
            if asset in _STABLES:
                capital += qty
                continue
            price = self._asset_price(asset)
            if price is not None:
                capital += qty * price
        return capital


    def _asset_price(self, asset: str) -> Decimal | None:
        """Precio en USD del activo, o None si no tiene mercado.

        Se omite con un WARNING (una vez por proceso y activo) el activo que no se puede
        valorar de forma persistente: sin mercado (BadSymbol o símbolo ausente
        de client.markets) o con el par listado pero sin precio (last nulo o no
        positivo). El bot solo opera símbolos que sabe cotizar, así que ese
        activo es tenencia ajena. Cualquier otro fallo sobre un par que existe
        (red, límite de tasa agotado, respuesta ilegible) sigue lanzando
        QuoteUnavailable: omitirlo a ratos descalibraría el pico de drawdown.

        Coste aceptado: si un par listado devuelve precio nulo de forma
        transitoria por un fallo puntual del exchange, se omite en silencio y la
        base de capital se mueve un poco mientras dure; la alternativa es que el
        bot deje de operar por completo por un activo que ni es suyo.
        """
        markets = getattr(self._client, "markets", None)
        for quote in _QUOTE_ASSETS:
            symbol = f"{asset}/{quote}"
            if markets and symbol not in markets:
                continue
            try:
                return self._last_price(symbol)
            except (_MarketMissing, _NoPrice):
                continue
        if asset not in self._warned_assets:
            self._warned_assets.add(asset)
            logger.warning("activo %s sin mercado cotizable; se omite del capital", asset)
        return None


class CexCsvSignalSource(SignalSource):
    """Señales desde un CSV `timestamp,trader_id,symbol,side,amount_usd`.

    El id es el hash de la fila completa: estable entre lecturas, porque el id
    de la señal alimenta el client_order_id que impide comprar dos veces lo mismo.
    """

    name = "cex"

    def __init__(self, csv_path: str | Path, market: Market = Market.CEX) -> None:
        self._path = Path(csv_path)
        self._market = market

    def _read(self) -> list[Signal]:
        signals: list[Signal] = []
        with self._path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = "\x1f".join(
                    row[c] or "" for c in ("timestamp", "trader_id", "symbol", "side", "amount_usd")
                )
                ts = datetime.fromisoformat(row["timestamp"].strip())
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                amount = (row["amount_usd"] or "").strip()
                signals.append(
                    Signal(
                        id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
                        trader_id=row["trader_id"].strip(),
                        market=self._market,
                        symbol=row["symbol"].strip(),
                        side=Side(row["side"].strip().lower()),
                        timestamp=ts,
                        trader_amount_usd=Decimal(amount) if amount else None,
                    )
                )
        return signals

    def fetch_trades(self, since: datetime | None) -> list[Signal]:
        signals = self._read()
        if since is not None:
            signals = [s for s in signals if s.timestamp > since]
        return sorted(signals, key=lambda s: s.timestamp)

    def list_followed(self) -> list[str]:
        return sorted({s.trader_id for s in self._read()})
