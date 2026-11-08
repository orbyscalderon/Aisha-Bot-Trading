"""Precio de SOL en dólares para valorar los swaps de Solana que no tocan ninguna stablecoin.

POR QUÉ EXISTE. Contra Helius real, una wallet dio 545 swaps y cero señales: 442 (el 81 %) se
omitieron por ``no_stable``. En Solana no se opera contra dólares, se opera contra SOL. El lector
exigía una pata de stablecoin para saber cuántos dólares movió el trader, así que descartaba
cuatro de cada cinco operaciones reales. Con el precio de SOL de esa hora, la pata de SOL sí se
puede valorar, y el precio no se inventa: sale de las velas de SOL/USDT que ya baja
``fetch-quotes`` de Binance.

Decisiones:

- **Nunca se mira al futuro.** Las cotizaciones llevan como sello el CIERRE de su vela (ver
  ``adapters/cex/history.py``), así que la vela sellada a las 10:00 no se conoce hasta las 10:00.
  Para un swap a las 09:30 se usa el cierre de las 09:00, no el de las 10:00. Es la misma regla
  que ``HistoricalSource.get_quote`` del backtest.
- **Un precio viejo no vale.** Si la última vela conocida queda más atrás que ``max_staleness``,
  no hay precio y el swap se omite. Rellenar un hueco con el último precio conocido es lo
  peligroso: SOL se mueve un 10 % en un día, así que un precio de ayer pondría un tamaño falso en
  la señal y el bot copiaría una operación con el importe equivocado. Mejor omitir y contarlo.
- **La tolerancia por defecto son 2 horas** porque ``fetch-quotes`` baja velas de 1h por defecto:
  el retraso normal llega a 59 minutos y dos horas aguantan además una vela ausente. Con velas
  diarias casi todo se omitiría por ``no_sol_price``, que es ruidoso y honesto, en vez de
  silenciosamente mal valorado.
- **Un precio no positivo o no finito es un error, no un dato más.** Un cero valoraría todas las
  señales en 0 USD y el bot no compraría nada sin decir por qué: es corrupción de datos.
- **El importe estimado no entra nunca en el id de la señal.** El id sale de la firma y de la
  wallet (``make_signal_id``). Si dependiera del precio, volver a bajar las cotizaciones daría
  otro id y el bot compraría dos veces el mismo swap.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from core.models import Quote

# Pares cuyo precio ES el precio de SOL en dólares. Un par como BONK/SOL no sirve: su precio es
# el de BONK, no el de SOL.
SOL_SYMBOLS = ("SOL/USDT", "SOL/USDC", "SOL/USD", "SOL/BUSD", "SOL/FDUSD")
MAX_STALENESS = timedelta(hours=2)


class SolPriceError(Exception):
    """Las cotizaciones de SOL no sirven para valorar; la CLI lo muestra como mensaje."""


def _aware(moment: datetime, what: str) -> datetime:
    """Exige zona horaria y normaliza a UTC.

    Sin esto, comparar una fecha ingenua con una consciente lanza ``TypeError`` en mitad de la
    valoración, lejos de la causa.
    """
    if not isinstance(moment, datetime):
        raise SolPriceError(f"{what}: se esperaba una fecha y llegó {type(moment).__name__}")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise SolPriceError(f"{what}: la fecha {moment.isoformat()} no lleva zona horaria")
    return moment.astimezone(timezone.utc)


class SolPriceTable:
    """Precio de SOL en dólares por momento, a partir de las cotizaciones de ``fetch-quotes``."""

    def __init__(
        self,
        quotes: Iterable[Quote],
        *,
        symbols: Iterable[str] = SOL_SYMBOLS,
        max_staleness: timedelta = MAX_STALENESS,
    ) -> None:
        if max_staleness < timedelta(0):
            raise SolPriceError(f"max_staleness no puede ser negativo: {max_staleness}")
        self._max_staleness = max_staleness
        wanted = {s.upper() for s in symbols}
        points: list[tuple[datetime, Decimal]] = []
        for quote in quotes:
            if (quote.symbol or "").upper() not in wanted:
                continue  # el precio de otro par no es el precio de SOL
            price = quote.price
            # El sello se formatea en crudo a propósito: si además le falta la zona horaria, el
            # error que importa es el del precio, no el de la fecha.
            if not isinstance(price, Decimal) or not price.is_finite():
                raise SolPriceError(
                    f"precio de SOL no finito en {quote.symbol} a {quote.timestamp!r}: {price!r}"
                )
            if price <= 0:
                raise SolPriceError(
                    f"precio de SOL no positivo ({price}) en {quote.symbol} a "
                    f"{quote.timestamp!r}: con él todas las señales contra SOL valdrían 0 USD y el "
                    "bot no compraría nada sin decir por qué"
                )
            points.append((_aware(quote.timestamp, "cotización de SOL"), price))
        # Estable: con el mismo sello repetido gana el último leído (sort es estable en Python).
        points.sort(key=lambda p: p[0])
        self._times = [p[0] for p in points]
        self._prices = [p[1] for p in points]

    def __len__(self) -> int:
        return len(self._times)

    def price_at(self, moment: datetime) -> Decimal | None:
        """Precio de SOL vigente en ``moment``, o ``None`` si no se conoce.

        Devuelve el cierre de la última vela sellada en ``moment`` o antes (jamás una posterior:
        sería un precio que aún no existía) y solo si no queda más atrás que ``max_staleness``.
        """
        when = _aware(moment, "momento del swap")
        if not self._times:
            return None
        i = bisect.bisect_right(self._times, when)
        if i == 0:
            return None  # el swap es anterior a la primera vela conocida
        if when - self._times[i - 1] > self._max_staleness:
            return None  # hueco en el histórico: no se rellena con el último precio conocido
        return self._prices[i - 1]
