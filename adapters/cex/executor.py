"""Ejecutor REAL contra un exchange centralizado vía ccxt. Dinero de verdad.

LO QUE MÁS IMPORTA DE ESTE FICHERO es qué excepción sale de cada fallo, porque
el motor decide con eso (ver core/engine.py y ports/market.py):

  - ``OrderRejected``  -> el intent se marca FAILED y la señal SE PUEDE REINTENTAR.
  - cualquier otra     -> el intent se queda en SENT, bloquea el reintento y avisa
                          como CRÍTICO para reconciliar a mano.

Por tanto ``OrderRejected`` solo puede salir cuando SABEMOS que la orden no se
ejecutó. Confundirse hacia ese lado manda la misma compra dos veces al mercado;
confundirse hacia el otro deja una señal sin operar hasta que alguien mire. Lo
segundo cuesta una oportunidad, lo primero cuesta dinero, así que ante la duda
este adaptador NO dice "rechazada".

La jerarquía de ccxt separa justo eso:
  - ``ccxt.ExchangeError``   -> el exchange CONTESTÓ que no (saldo insuficiente,
                                orden inválida, par inexistente, clave sin permiso).
                                La orden no entró: rechazo definitivo.
  - ``ccxt.OperationFailed`` -> la petición falló sin respuesta clara (timeout, red
                                caída, exchange no disponible, límite de tasa).
                                No sabemos si entró: ambiguo.

NO SE REINTENTA NADA AQUÍ, a propósito, ni siquiera un límite de tasa. Reintentar
un envío es exactamente lo que la idempotencia intenta evitar. El client_order_id
viaja al exchange y Binance rechaza un duplicado, pero esa protección solo dura
mientras la orden sigue viva: una orden de mercado se llena al instante, así que la
ventana es de milisegundos y no se puede confiar en ella. La fuente de precios sí
reintenta (adapters/cex/source.py) porque leer dos veces no cuesta nada.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import ccxt

from core.models import Fill, Order, Side
from ports.market import OrderExecutor, OrderRejected, ZeroQuantity

logger = logging.getLogger(__name__)

# Activos que valen 1 USD: si la comisión se cobra en uno de ellos, su coste YA es
# el coste en dólares y no hace falta convertirlo.
_STABLES = frozenset({"USDT", "USDC", "USD", "BUSD", "FDUSD"})


class AmbiguousOrder(Exception):
    """La orden salió y no sabemos en qué quedó. NO hereda de OrderRejected a propósito.

    El motor la trata como ambigua: el intent se queda en SENT y avisa como crítico.
    """


def _to_decimal(value: Any, what: str) -> Decimal:
    """Decimal(str(x)) siempre: Decimal(float) arrastra el error binario del float."""
    if value is None or isinstance(value, bool):
        raise AmbiguousOrder(f"{what}: el exchange no devolvió el dato")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AmbiguousOrder(f"{what}: valor no numérico {value!r}") from exc
    if not result.is_finite():
        raise AmbiguousOrder(f"{what}: valor no finito {value!r}")
    return result


class CexExecutor(OrderExecutor):
    """Órdenes a mercado contra un exchange de ccxt ya autenticado.

    Se construye con el cliente hecho (no con claves) para que quien lo monta
    decida testnet o real, y para que los tests no toquen la red.
    """

    def __init__(self, client: Any, *, quantity_precision: int) -> None:
        self._client = client
        # Red de seguridad: la precisión del propio mercado manda cuando ccxt la
        # trae cargada; esto solo se usa si no la hay.
        self._fallback_quantum = Decimal(1).scaleb(-quantity_precision)

    # --- envío -----------------------------------------------------------------

    def submit_buy(self, order: Order) -> Fill:
        """Compra por IMPORTE, no por cantidad.

        Los límites de riesgo del bot están en dólares (hard_max_order_usd,
        max_usd_per_day). Calcular aquí una cantidad a partir del precio de
        referencia, que ya es viejo cuando la orden llega, haría que una subida
        entre medias gastara MÁS dólares de los autorizados. Pidiendo el importe
        exacto, el exchange ajusta la cantidad y el tope en dólares se respeta.
        """
        if order.amount_usd <= 0:  # defensa; Order.__post_init__ ya lo garantiza
            raise ZeroQuantity(f"{order.client_order_id}: importe no positivo {order.amount_usd}")
        raw = self._send(
            self._client.create_market_buy_order_with_cost,
            order.symbol,
            float(order.amount_usd),
            self._params(order),
        )
        return self._fill_from(raw, order, Side.BUY)

    def submit_sell(self, order: Order) -> Fill:
        """Vende la CANTIDAD de la posición, redondeada hacia ABAJO.

        Hacia abajo y nunca hacia arriba: redondear hacia arriba pide vender más
        de lo que se tiene y el exchange rechaza la orden entera, dejando la
        posición abierta justo cuando se la quería cerrar (p. ej. en un stop-loss).
        """
        assert order.quantity is not None  # garantizado por Order.__post_init__
        quantity = self._adjust_quantity(order.symbol, order.quantity)
        if quantity <= 0:
            raise ZeroQuantity(
                f"{order.client_order_id}: cantidad cero al ajustar a la precisión "
                f"del mercado ({order.symbol}, tenía {order.quantity})"
            )
        raw = self._send(
            self._client.create_market_sell_order,
            order.symbol,
            float(quantity),
            self._params(order),
        )
        return self._fill_from(raw, order, Side.SELL)

    def _params(self, order: Order) -> dict[str, str]:
        """El client_order_id viaja al exchange: deja rastro para reconciliar.

        Si el bot se cae tras enviar, ese identificador es lo que permite
        preguntarle al exchange si la orden llegó, en vez de adivinarlo.
        """
        return {"clientOrderId": order.client_order_id}

    def _send(self, fn: Any, *args: Any) -> Any:
        """Traduce los fallos de ccxt a la semántica del puerto. Sin reintentos."""
        try:
            return fn(*args)
        except ccxt.OperationFailed as exc:
            # Timeout, red, exchange caído, límite de tasa: la orden PUEDE haber entrado.
            raise AmbiguousOrder(f"envío de resultado incierto: {exc!r}") from exc
        except ccxt.ExchangeError as exc:
            # El exchange contestó que no. La orden no entró.
            raise OrderRejected(f"el exchange rechazó la orden: {exc!r}") from exc
        except Exception as exc:  # noqa: BLE001  — parseo interno de ccxt, sockets
            # Desconocido: por definición ambiguo. Nunca OrderRejected.
            raise AmbiguousOrder(f"fallo inesperado al enviar: {exc!r}") from exc

    # --- lectura de la respuesta ------------------------------------------------

    def _step(self, symbol: str) -> Decimal | None:
        """Tamaño del lote del mercado, o None si no se puede saber.

        CUIDADO: ccxt expresa `precision.amount` de DOS maneras incompatibles según
        el exchange, y confundirlas es catastrófico:

          - DECIMAL_PLACES (modo 2): un número de decimales. 5 significa 0.00001.
          - TICK_SIZE (modo 4): el paso YA es el valor. 1e-05 significa 0.00001.

        Binance y Kraken usan TICK_SIZE. Leer ese 1e-05 como "0 decimales" daría un
        lote de 1 BTC entero: una posición de 0,002 BTC se redondearía a cero al
        vender, sería imposible de cerrar y el stop-loss no saltaría NUNCA.
        """
        markets = getattr(self._client, "markets", None) or {}
        market = markets.get(symbol) or {}
        bruto = (market.get("precision") or {}).get("amount")
        if bruto is None or isinstance(bruto, bool):
            return None
        try:
            valor = Decimal(str(bruto))
        except InvalidOperation:
            return None
        if not valor.is_finite() or valor <= 0:
            return None
        if getattr(self._client, "precisionMode", None) == getattr(ccxt, "TICK_SIZE", 4):
            return valor
        if valor == valor.to_integral_value():
            return Decimal(1).scaleb(-int(valor))
        # Un valor fraccionario declarándose "decimales" no tiene sentido. Tomarlo
        # como paso es lo conservador: nunca agranda la cantidad.
        return valor

    def _adjust_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        """Baja la cantidad al múltiplo del lote. Siempre hacia abajo, nunca hacia arriba."""
        paso = self._step(symbol)
        if paso is None:
            return quantity.quantize(self._fallback_quantum, rounding=ROUND_DOWN)
        return (quantity / paso).to_integral_value(rounding=ROUND_DOWN) * paso

    def _fill_from(self, raw: Any, order: Order, side: Side) -> Fill:
        """Convierte la respuesta de ccxt en un Fill, o falla como ambiguo.

        Todo lo que no se entienda es AMBIGUO, nunca un rechazo: si el exchange
        contestó algo, la orden salió, y no poder leer su respuesta no significa
        que no se ejecutara.
        """
        if not isinstance(raw, dict):
            raise AmbiguousOrder(
                f"{order.client_order_id}: respuesta ilegible "
                f"({type(raw).__name__} {repr(raw)[:120]})"
            )

        filled = _to_decimal(raw.get("filled"), f"{order.client_order_id}: cantidad ejecutada")
        if filled <= 0:
            # Una orden a mercado que no llena nada no es normal. Puede quedar viva.
            raise AmbiguousOrder(
                f"{order.client_order_id}: el exchange no reporta ejecución "
                f"(filled={filled}, status={raw.get('status')!r})"
            )

        price = self._executed_price(raw, order)
        # Lo que se RECIBE, no lo que se ejecuta. Binance cobra la comisión de una compra
        # en el activo comprado, así que de 0,00033 BTC ejecutados entran 0,00032967. El
        # motor guarda esta cantidad en la posición y luego intenta venderla entera: con
        # la cifra bruta, el exchange contesta "saldo insuficiente", eso es un rechazo
        # definitivo, se reintenta y vuelve a fallar. La posición no se podría cerrar
        # NUNCA y el stop-loss no saltaría. Verificado contra el testnet de Binance.
        # Solo en la COMPRA: ahí `filled` es lo que entra y la comisión lo recorta. En una
        # venta `filled` es lo que salió de la cuenta, y restarle algo mentiría sobre la
        # operación; además Binance cobra la venta en la moneda de cotización, no en BTC.
        if side is Side.BUY:
            filled -= self._base_fee(raw, order)
            if filled <= 0:
                raise AmbiguousOrder(
                    f"{order.client_order_id}: la comisión se come la cantidad entera "
                    f"({raw.get('filled')} ejecutado)"
                )
        status = raw.get("status")
        if status != "closed":
            # Llenado PARCIAL: se registra lo ejecutado, que es real, y se avisa.
            # Mentir aquí descuadraría la posición contra el saldo del exchange.
            logger.warning(
                "%s: orden en estado %r con %s ejecutado; se registra lo ejecutado",
                order.client_order_id, status, filled,
            )

        return Fill(
            id=str(raw.get("id") or order.client_order_id),
            client_order_id=order.client_order_id,
            market=order.market,
            symbol=order.symbol,
            side=side,
            price=price,
            quantity=filled,
            fee_usd=self._fee_usd(raw, price, order),
            timestamp=self._timestamp(raw),
            position_id=order.position_id,
        )

    def _executed_price(self, raw: dict, order: Order) -> Decimal:
        """Precio MEDIO realmente pagado, no el de referencia.

        Se prefiere `average` porque una orden a mercado puede barrer varios
        niveles del libro: `price` sería solo el primero y haría parecer mejor
        la ejecución de lo que fue.
        """
        for campo in ("average", "price"):
            valor = raw.get(campo)
            if valor is not None:
                precio = _to_decimal(valor, f"{order.client_order_id}: precio ({campo})")
                if precio > 0:
                    return precio
        coste, cantidad = raw.get("cost"), raw.get("filled")
        if coste is not None and cantidad:
            precio = _to_decimal(coste, "coste") / _to_decimal(cantidad, "cantidad")
            if precio > 0:
                return precio
        raise AmbiguousOrder(
            f"{order.client_order_id}: la orden se ejecutó pero sin precio legible"
        )

    def _fee_usd(self, raw: dict, price: Decimal, order: Order) -> Decimal:
        """Comisión en dólares, sumando todas las que reporte el exchange.

        La comisión puede cobrarse en la moneda de cotización (USDT: ya son
        dólares), en el activo comprado (se valora al precio ejecutado) o en un
        tercer activo como BNB, que no se puede convertir sin otra consulta. En
        ese último caso se cuenta 0 y se AVISA: inventar un cambio metería un
        número falso en la contabilidad, y un cero avisado es auditable.
        """
        base, _, cotizacion = order.symbol.partition("/")
        total = Decimal(0)
        for coste, moneda in self._fees(raw, order):
            if moneda in _STABLES or moneda == cotizacion.upper():
                total += coste
            elif moneda == base.upper():
                total += coste * price
            else:
                logger.warning(
                    "%s: comisión de %s %s sin precio en dólares; se cuenta 0 y la "
                    "contabilidad del bot queda por debajo del coste real",
                    order.client_order_id, coste, moneda,
                )
        return total

    def _base_fee(self, raw: dict, order: Order) -> Decimal:
        """Comisión cobrada en el ACTIVO negociado, que reduce lo que se recibe."""
        base = order.symbol.partition("/")[0].upper()
        return sum(
            (coste for coste, moneda in self._fees(raw, order) if moneda == base),
            Decimal(0),
        )

    def _fees(self, raw: dict, order: Order) -> list[tuple[Decimal, str]]:
        """Comisiones reportadas, como (coste, moneda). ccxt usa 'fees' o 'fee'."""
        comisiones = raw.get("fees")
        if not comisiones:
            una = raw.get("fee")
            comisiones = [una] if una else []
        salida: list[tuple[Decimal, str]] = []
        for comision in comisiones:
            if not isinstance(comision, dict) or comision.get("cost") is None:
                continue
            salida.append((
                _to_decimal(comision["cost"], f"{order.client_order_id}: comisión"),
                (comision.get("currency") or "").upper(),
            ))
        return salida

    def _timestamp(self, raw: dict) -> datetime:
        """Hora del exchange si la da; si no, la nuestra.

        ccxt la devuelve en milisegundos. Una respuesta sin hora no invalida una
        ejecución real, así que se usa el reloj local en vez de fallar.
        """
        milisegundos = raw.get("timestamp")
        if milisegundos is not None:
            try:
                return datetime.fromtimestamp(int(milisegundos) / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError, OverflowError):
                logger.warning("marca de tiempo ilegible del exchange: %r", milisegundos)
        return datetime.now(timezone.utc)
