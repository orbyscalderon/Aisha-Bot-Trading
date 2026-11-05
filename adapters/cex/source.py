"""Fuente de precios (ccxt) y de señales (CSV) para exchanges centralizados."""

from __future__ import annotations

import csv
import hashlib
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import ccxt

from core.models import Market, Quote, Side, Signal
from ports.market import MarketDataSource, QuoteUnavailable
from ports.signal_source import SignalSource

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0

# Activos que valen 1 USD; el resto se valora con su par contra USDT.
_STABLES = frozenset({"USDT", "USDC", "USD", "BUSD", "FDUSD"})
_QUOTE_ASSET = "USDT"


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
        self._init(client, sleep, max_attempts)

    def _init(self, client: Any, sleep: Callable[[float], None], max_attempts: int) -> None:
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts

    @classmethod
    def from_client(
        cls,
        client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> "CexDataSource":
        """Construye la fuente con un cliente ya hecho (los tests no tocan la red)."""
        self = cls.__new__(cls)
        self._init(client, sleep, max_attempts)
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
            except ccxt.BaseError as exc:
                raise QuoteUnavailable(f"error de ccxt: {exc}") from exc
        raise QuoteUnavailable("sin intentos disponibles")  # max_attempts <= 0

    def _last_price(self, symbol: str) -> Decimal:
        ticker = self._call(self._client.fetch_ticker, symbol)
        raw = ticker.get("last") if isinstance(ticker, dict) else None
        price = _to_decimal(raw, f"precio de {symbol}")
        if price <= 0:
            raise QuoteUnavailable(f"precio no positivo para {symbol}: {price}")
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
        alguno no se puede valorar se lanza QuoteUnavailable en vez de
        infravalorar el capital en silencio.
        """
        balance = self._call(self._client.fetch_balance)
        totals = balance.get("total") if isinstance(balance, dict) else None
        if not isinstance(totals, dict):
            raise QuoteUnavailable("balance sin campo 'total'")
        capital = Decimal(0)
        for asset, amount in totals.items():
            qty = _to_decimal(amount, f"saldo de {asset}")
            if qty == 0:
                continue
            if asset in _STABLES:
                capital += qty
            else:
                capital += qty * self._last_price(f"{asset}/{_QUOTE_ASSET}")
        return capital


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
