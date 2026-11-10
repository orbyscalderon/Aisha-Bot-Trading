"""Tests del ejecutor real contra un CEX.

El riesgo de este adaptador no es calcular mal un precio: es DECIR QUE UNA ORDEN
FALLÓ CUANDO PUDO EJECUTARSE. El motor reintenta lo que llega como OrderRejected,
así que una traducción floja de los errores de ccxt manda la misma compra dos
veces al mercado. Por eso la mitad de este fichero son tests sobre qué excepción
sale de cada fallo, y no sobre números.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import ccxt
import pytest

from adapters.cex.executor import AmbiguousOrder, CexExecutor
from core.models import Fill, Market, Order, Side
from ports.market import OrderExecutor, OrderRejected, ZeroQuantity

COID = "ct-" + "a" * 32


def compra(amount="100", symbol="BTC/USDT"):
    return Order(COID, Market.CEX, symbol, Side.BUY, Decimal(amount), Decimal("50000"))


def venta(quantity="0.5", symbol="BTC/USDT"):
    return Order(
        COID, Market.CEX, symbol, Side.SELL, Decimal("100"), Decimal("50000"),
        position_id="pos-1", quantity=Decimal(quantity),
    )


RESPUESTA = {
    "id": "99", "status": "closed", "filled": "0.002", "average": "50000",
    "cost": "100", "timestamp": 1731168000000,
    "fee": {"cost": "0.1", "currency": "USDT"},
}


class FakeClient:
    """Cliente de ccxt de mentira. Registra CÓMO se le llamó, no solo cuántas veces."""

    def __init__(self, respuesta=None, error=None, markets=None, precision_mode=None):
        self._respuesta = RESPUESTA if respuesta is None else respuesta
        self._error = error
        self.markets = markets
        if precision_mode is not None:
            self.precisionMode = precision_mode
        self.llamadas: list[tuple] = []

    def _registrar(self, nombre, *args, **kwargs):
        self.llamadas.append((nombre, args, kwargs))
        if self._error is not None:
            raise self._error
        return self._respuesta

    def create_market_buy_order_with_cost(self, symbol, cost, params={}):
        return self._registrar("buy", symbol, cost, params)

    def create_market_sell_order(self, symbol, amount, params={}):
        return self._registrar("sell", symbol, amount, params)


def ejecutor(client, precision=8):
    return CexExecutor(client, quantity_precision=precision)


# --- la frontera que de verdad importa: qué excepción sale ---------------------
# ports/market.py: solo OrderRejected permite reintentar. Todo lo demás deja el
# intent en SENT. Estos tests fijan esa frontera error por error.


@pytest.mark.parametrize("error", [
    ccxt.InsufficientFunds("saldo insuficiente"),
    ccxt.InvalidOrder("orden invalida"),
    ccxt.BadSymbol("par inexistente"),
    ccxt.AuthenticationError("clave mala"),
    ccxt.PermissionDenied("sin permiso de trading"),
    ccxt.ExchangeError("que no"),
])
def test_si_el_exchange_CONTESTA_que_no_es_rechazo_definitivo(error):
    """El exchange respondió: la orden no entró. Reintentarla es seguro y deseable."""
    with pytest.raises(OrderRejected):
        ejecutor(FakeClient(error=error)).submit_buy(compra())


@pytest.mark.parametrize("error", [
    ccxt.RequestTimeout("sin respuesta"),
    ccxt.NetworkError("red caida"),
    ccxt.ExchangeNotAvailable("mantenimiento"),
    ccxt.RateLimitExceeded("demasiadas peticiones"),
    ccxt.OperationFailed("fallo"),
])
def test_un_fallo_SIN_respuesta_NUNCA_se_marca_como_rechazo(error):
    """Este es EL test del fichero.

    Un timeout no dice que la orden no entrara: dice que no sabemos. Si saliera
    como OrderRejected, el motor marcaría FAILED, reintentaría la señal y
    compraría dos veces lo mismo. Tiene que salir como otra cosa.
    """
    with pytest.raises(Exception) as excinfo:
        ejecutor(FakeClient(error=error)).submit_buy(compra())
    assert not isinstance(excinfo.value, OrderRejected), (
        f"{type(error).__name__} se tradujo a un rechazo: el motor reintentaria "
        "una orden que pudo ejecutarse"
    )


def test_un_error_desconocido_tampoco_es_rechazo():
    """Lo que no se reconoce es, por definición, incierto."""
    with pytest.raises(Exception) as excinfo:
        ejecutor(FakeClient(error=RuntimeError("vete a saber"))).submit_buy(compra())
    assert not isinstance(excinfo.value, OrderRejected)


def test_lo_mismo_vale_para_las_ventas():
    with pytest.raises(OrderRejected):
        ejecutor(FakeClient(error=ccxt.InsufficientFunds("x"))).submit_sell(venta())
    with pytest.raises(Exception) as excinfo:
        ejecutor(FakeClient(error=ccxt.RequestTimeout("x"))).submit_sell(venta())
    assert not isinstance(excinfo.value, OrderRejected)


def test_NO_se_reintenta_un_envio():
    """Reintentar un envío es justo lo que la idempotencia intenta evitar: el
    primer intento pudo llegar. La fuente de precios sí reintenta; esto no."""
    c = FakeClient(error=ccxt.RateLimitExceeded("429"))
    with pytest.raises(Exception):
        ejecutor(c).submit_buy(compra())
    assert len(c.llamadas) == 1, "se envió la orden más de una vez"


# --- cómo se llama al exchange -------------------------------------------------


def test_la_compra_pide_un_IMPORTE_no_una_cantidad():
    """Los topes de riesgo del bot están en dólares. Pedir cantidad calculada
    desde un precio viejo gastaría más de lo autorizado si el precio sube."""
    c = FakeClient()
    ejecutor(c).submit_buy(compra("250"))
    nombre, args, _ = c.llamadas[0]
    assert nombre == "buy" and args[0] == "BTC/USDT" and args[1] == 250.0


def test_la_venta_se_llama_con_la_firma_REAL_de_ccxt():
    """create_market_sell_order es (symbol, amount, params): tres argumentos.

    Un argumento de más da TypeError, que este adaptador convierte en fallo
    ambiguo. Resultado: TODA venta quedaría en SENT y ningún stop-loss se
    ejecutaría jamás. Este test existe porque ese fallo llegó a estar escrito.
    """
    c = FakeClient()
    ejecutor(c).submit_sell(venta("0.5"))
    nombre, args, _ = c.llamadas[0]
    assert nombre == "sell"
    assert len(args) == 3, f"se pasaron {len(args)} argumentos; ccxt acepta 3"
    assert args[0] == "BTC/USDT" and args[1] == 0.5


def test_el_client_order_id_viaja_al_exchange():
    """Sin él no se puede preguntar al exchange si la orden llegó tras una caída."""
    for metodo, orden in (("submit_buy", compra()), ("submit_sell", venta())):
        c = FakeClient()
        getattr(ejecutor(c), metodo)(orden)
        assert c.llamadas[0][1][-1] == {"clientOrderId": COID}


# --- cantidades ----------------------------------------------------------------


def test_la_venta_redondea_hacia_ABAJO():
    """Hacia arriba pediría vender más de lo que se tiene y el exchange tumbaría
    la orden entera, dejando abierta la posición que se quería cerrar."""
    c = FakeClient()
    ejecutor(c, precision=2).submit_sell(venta("0.567"))
    assert c.llamadas[0][1][1] == 0.56


def test_manda_la_precision_del_MERCADO_sobre_la_configurada():
    """El exchange es la autoridad sobre su propio lote mínimo."""
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": 3}}})
    ejecutor(c, precision=8).submit_sell(venta("0.56789"))
    assert c.llamadas[0][1][1] == 0.567


def test_sin_precision_del_mercado_se_usa_la_configurada():
    c = FakeClient(markets={})
    ejecutor(c, precision=1).submit_sell(venta("0.56"))
    assert c.llamadas[0][1][1] == 0.5


def test_la_precision_de_BINANCE_es_un_PASO_no_un_numero_de_decimales():
    """El fallo más caro que ha tenido este fichero.

    ccxt expresa precision.amount de dos maneras incompatibles. Binance y Kraken
    usan TICK_SIZE, donde el valor YA es el paso: 1e-05 significa 0,00001. Leerlo
    como "0 decimales" da un lote de 1 BTC entero, redondea a CERO una posición de
    0,002 BTC y la deja imposible de vender: el stop-loss no saltaría nunca.

    Los valores de este test son los que devolvió el testnet de Binance de verdad.
    """
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": 1e-05}}},
                   precision_mode=ccxt.TICK_SIZE)
    ejecutor(c).submit_sell(venta("0.0023456"))
    assert c.llamadas[0][1][1] == 0.00234


def test_con_TICK_SIZE_una_posicion_pequena_SIGUE_siendo_vendible():
    """La consecuencia que de verdad importa: que se pueda cerrar."""
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": 1e-05}}},
                   precision_mode=ccxt.TICK_SIZE)
    fill = ejecutor(c).submit_sell(venta("0.002"))
    assert c.llamadas[0][1][1] == 0.002
    assert fill.side is Side.SELL


def test_un_paso_que_no_es_potencia_de_diez_tambien_vale():
    """Algunos mercados van de 5 en 5, o de 0,25 en 0,25."""
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": 0.25}}},
                   precision_mode=ccxt.TICK_SIZE)
    ejecutor(c).submit_sell(venta("1.7"))
    assert c.llamadas[0][1][1] == 1.5


def test_con_DECIMAL_PLACES_el_valor_son_decimales():
    """La otra convención: ahí un 5 sí significa cinco decimales."""
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": 5}}},
                   precision_mode=ccxt.DECIMAL_PLACES)
    ejecutor(c).submit_sell(venta("0.0023456"))
    assert c.llamadas[0][1][1] == 0.00234


def test_una_precision_ilegible_cae_en_la_configurada_sin_romper():
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": "ni idea"}}})
    ejecutor(c, precision=3).submit_sell(venta("0.56789"))
    assert c.llamadas[0][1][1] == 0.567


def test_una_precision_de_cero_no_se_toma_en_serio():
    """Un paso de cero dividiría por cero; un lote de cero no significa nada."""
    c = FakeClient(markets={"BTC/USDT": {"precision": {"amount": 0}}},
                   precision_mode=ccxt.TICK_SIZE)
    ejecutor(c, precision=3).submit_sell(venta("0.56789"))
    assert c.llamadas[0][1][1] == 0.567


def test_una_cantidad_que_se_queda_en_cero_al_redondear_no_se_envia():
    c = FakeClient()
    with pytest.raises(ZeroQuantity):
        ejecutor(c, precision=2).submit_sell(venta("0.001"))
    assert c.llamadas == [], "se envió una orden de cantidad cero"


# --- lectura de la respuesta ---------------------------------------------------


def test_devuelve_un_fill_con_lo_realmente_ejecutado():
    fill = ejecutor(FakeClient()).submit_buy(compra())
    assert isinstance(fill, Fill)
    assert fill.quantity == Decimal("0.002") and fill.price == Decimal("50000")
    assert fill.client_order_id == COID and fill.side is Side.BUY
    assert fill.market is Market.CEX and fill.id == "99"


def test_la_venta_conserva_el_position_id():
    """Sin él, el fill no se puede atar a la posición que cierra."""
    assert ejecutor(FakeClient()).submit_sell(venta()).position_id == "pos-1"


def test_se_prefiere_el_precio_MEDIO_al_primero_del_libro():
    """Una orden a mercado barre varios niveles: 'price' haría parecer mejor la
    ejecución de lo que fue."""
    c = FakeClient({**RESPUESTA, "average": "51000", "price": "50000"})
    assert ejecutor(c).submit_buy(compra()).price == Decimal("51000")


def test_sin_precio_medio_se_usa_el_coste_entre_la_cantidad():
    c = FakeClient({"id": "1", "status": "closed", "filled": "2", "cost": "100"})
    assert ejecutor(c).submit_buy(compra()).price == Decimal("50")


def test_una_ejecucion_sin_precio_legible_es_AMBIGUA_no_un_rechazo():
    """Se ejecutó algo: decir 'rechazada' haría que se comprara otra vez."""
    c = FakeClient({"id": "1", "status": "closed", "filled": "2"})
    with pytest.raises(AmbiguousOrder):
        ejecutor(c).submit_buy(compra())


def test_que_no_conste_ejecucion_es_ambiguo():
    """Una orden a mercado sin llenar no es normal: puede seguir viva."""
    c = FakeClient({**RESPUESTA, "filled": "0"})
    with pytest.raises(Exception) as excinfo:
        ejecutor(c).submit_buy(compra())
    assert not isinstance(excinfo.value, OrderRejected)


def test_una_respuesta_ilegible_es_ambigua():
    with pytest.raises(AmbiguousOrder):
        ejecutor(FakeClient("vaya cosa")).submit_buy(compra())


def test_un_llenado_PARCIAL_registra_lo_ejecutado(caplog):
    """Lo ejecutado es real. Descartarlo descuadraría la posición contra el saldo."""
    c = FakeClient({**RESPUESTA, "status": "open", "filled": "0.001"})
    fill = ejecutor(c).submit_buy(compra())
    assert fill.quantity == Decimal("0.001")
    assert "open" in caplog.text


# --- comisiones ----------------------------------------------------------------


def test_la_comision_en_la_moneda_de_cotizacion_ya_son_dolares():
    assert ejecutor(FakeClient()).submit_buy(compra()).fee_usd == Decimal("0.1")


def test_la_comision_en_el_activo_comprado_se_valora_al_precio_ejecutado():
    c = FakeClient({**RESPUESTA, "fee": {"cost": "0.0001", "currency": "BTC"}})
    assert ejecutor(c).submit_buy(compra()).fee_usd == Decimal("5")  # 0.0001 * 50000


def test_una_comision_en_otra_moneda_cuenta_CERO_y_avisa(caplog):
    """Inventar un cambio para BNB metería un número falso en la contabilidad.
    Un cero avisado es auditable; un número inventado, no."""
    c = FakeClient({**RESPUESTA, "fee": {"cost": "0.01", "currency": "BNB"}})
    assert ejecutor(c).submit_buy(compra()).fee_usd == Decimal("0")
    assert "BNB" in caplog.text


def test_varias_comisiones_se_suman():
    c = FakeClient({**RESPUESTA, "fee": None, "fees": [
        {"cost": "0.1", "currency": "USDT"}, {"cost": "0.2", "currency": "USDT"},
    ]})
    assert ejecutor(c).submit_buy(compra()).fee_usd == Decimal("0.3")


def test_sin_comision_reportada_es_cero():
    c = FakeClient({k: v for k, v in RESPUESTA.items() if k != "fee"})
    assert ejecutor(c).submit_buy(compra()).fee_usd == Decimal("0")


# --- fechas --------------------------------------------------------------------


def test_se_usa_la_hora_del_exchange():
    fill = ejecutor(FakeClient()).submit_buy(compra())
    assert fill.timestamp == datetime(2024, 11, 9, 16, 0, tzinfo=timezone.utc)


def test_una_hora_ilegible_no_tumba_una_ejecucion_real():
    """La orden se ejecutó: perderla por una fecha mala sería mucho peor."""
    c = FakeClient({**RESPUESTA, "timestamp": "ayer"})
    assert ejecutor(c).submit_buy(compra()).timestamp.tzinfo is timezone.utc


# --- contrato ------------------------------------------------------------------


def test_cumple_el_puerto():
    assert isinstance(ejecutor(FakeClient()), OrderExecutor)


def test_los_importes_no_pasan_por_float():
    """Decimal(str(x)) en todo: Decimal(0.1) arrastra el error binario del float."""
    c = FakeClient({**RESPUESTA, "average": "0.1", "filled": "3"})
    assert ejecutor(c).submit_buy(compra()).price == Decimal("0.1")
