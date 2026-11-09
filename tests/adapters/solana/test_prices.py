"""Tests de los precios de tokens de Solana: histórico (GeckoTerminal) y actual (Jupiter).

Sin esto, las 58 señales reales de Solana no se pueden backtestear: Binance no cotiza BONK.

Dos trampas de la API real que estos tests vigilan, porque cualquiera de las dos daría
precios falsos sin avisar:

1. GeckoTerminal sella las velas por APERTURA; todo el proyecto las sella por CIERRE.
2. La primera vela que devuelve es la hora EN CURSO, aún sin cerrar.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from adapters.solana.prices import (
    JupiterPriceSource,
    SolanaPriceError,
    fetch_token_quotes,
    pick_pool,
)
from ports.market import MarketUnpriceable, QuoteUnavailable

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
HORA = 3600
T0 = 1_793_000_000 - (1_793_000_000 % HORA)  # alineado a la hora en punto
AHORA = datetime.fromtimestamp(T0 + 5 * HORA, tz=timezone.utc)


def pool(pid: str, liquidez: str) -> dict:
    return {"id": f"solana_{pid}", "type": "pool",
            "attributes": {"name": f"{pid} / SOL", "reserve_in_usd": liquidez}}


def vela(hora_apertura: int, cierre) -> list:
    return [hora_apertura, 1.0, 2.0, 0.5, cierre, 1000.0]


class Gecko:
    """GeckoTerminal falso. Las velas se devuelven de la más NUEVA a la más antigua."""

    def __init__(self, pools=None, velas=None, status=200, error=None):
        self.pools = pools if pools is not None else [pool("P1", "500000")]
        self.velas = velas if velas is not None else []
        self.status = status
        self.error = error
        self.peticiones: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        if self.error:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, text="no")
        # /ohlcv primero: la URL de velas es /pools/<id>/ohlcv/... y tambien lleva "/pools".
        if "/ohlcv/" not in request.url.path:
            return httpx.Response(200, json={"data": self.pools})
        antes = request.url.params.get("before_timestamp")
        limite = int(request.url.params.get("limit", 1000))
        filtradas = [v for v in self.velas if antes is None or v[0] < int(antes)]
        return httpx.Response(200, json={
            "data": {"attributes": {"ohlcv_list": filtradas[:limite]}}})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def traer(server, **kw):
    kw.setdefault("since", datetime.fromtimestamp(T0 - HORA, tz=timezone.utc))
    kw.setdefault("now", AHORA)
    kw.setdefault("sleep", lambda _s: None)
    return fetch_token_quotes(server.client(), BONK, "BONK/USDC", **kw)


# --- elección de pool ---

def test_se_elige_el_pool_con_mas_liquidez():
    """El precio de un pool con poca liquidez se mueve con poco dinero: es manipulable."""
    pid, liq = pick_pool([pool("CHICO", "1000"), pool("GRANDE", "900000"), pool("MEDIO", "5000")])
    assert pid == "GRANDE" and liq == Decimal("900000")


def test_un_pool_sin_liquidez_legible_no_gana():
    pid, _ = pick_pool([pool("ROTO", "no-es-un-numero"), pool("BUENO", "100")])
    assert pid == "BUENO"


def test_sin_ningun_pool_se_dice_claramente():
    with pytest.raises(SolanaPriceError, match="sin pools"):
        pick_pool([])


# --- las dos trampas ---

def test_el_sello_pasa_de_apertura_a_cierre():
    """GeckoTerminal manda la apertura. La vela abierta a las 10:00 no se conoce hasta las
    11:00; sellarla a las 10:00 sería dar por sabido un precio una hora antes de existir."""
    server = Gecko(velas=[vela(T0, 3.0)])
    quotes = traer(server)
    assert len(quotes) == 1
    assert quotes[0].timestamp == datetime.fromtimestamp(T0 + HORA, tz=timezone.utc)


def test_la_vela_en_curso_se_descarta():
    """Su 'cierre' es el precio de este instante y cambiaría en la siguiente consulta."""
    en_curso = int(AHORA.timestamp())          # abre ahora: cierra en el futuro
    cerrada = en_curso - HORA
    quotes = traer(Gecko(velas=[vela(en_curso, 9.9), vela(cerrada, 1.1)]))
    assert [q.price for q in quotes] == [Decimal("1.1")]


def test_la_vela_que_acaba_de_cerrar_si_entra():
    justo = int(AHORA.timestamp()) - HORA  # cierra exactamente ahora
    assert len(traer(Gecko(velas=[vela(justo, 2.2)]))) == 1


# --- precisión ---

def test_un_precio_diminuto_no_pierde_precision():
    """BONK vale 0,0000027 USD. Pasar por float perdería cifras justo donde están todas."""
    server = Gecko(velas=[vela(T0, 2.702427074091977e-06)])
    assert traer(server)[0].price == Decimal("0.000002702427074091977")


def test_el_precio_nunca_se_construye_desde_un_float():
    q = traer(Gecko(velas=[vela(T0, 0.1)]))[0]
    assert q.price == Decimal("0.1")          # y no 0.1000000000000000055511151231257827


# --- orden y filtrado ---

def test_las_cotizaciones_salen_de_la_mas_antigua_a_la_mas_nueva():
    server = Gecko(velas=[vela(T0 + 2 * HORA, 3.0), vela(T0 + HORA, 2.0), vela(T0, 1.0)])
    assert [q.price for q in traer(server)] == [Decimal("1"), Decimal("2"), Decimal("3")]


def test_no_se_devuelve_nada_anterior_a_since():
    server = Gecko(velas=[vela(T0 + HORA, 2.0), vela(T0, 1.0), vela(T0 - 5 * HORA, 0.5)])
    quotes = traer(server, since=datetime.fromtimestamp(T0, tz=timezone.utc))
    assert [q.price for q in quotes] == [Decimal("1"), Decimal("2")]


def test_el_simbolo_es_el_que_se_pide_no_el_mint():
    """Tiene que coincidir con el de las señales o el backtest no emparejaría nada."""
    assert traer(Gecko(velas=[vela(T0, 1.0)]))[0].symbol == "BONK/USDC"


# --- velas inservibles ---

@pytest.mark.parametrize("malo", [
    [T0, 1.0, 2.0, 0.5, None, 1.0],      # sin cierre
    [T0, 1.0, 2.0, 0.5, "x", 1.0],       # cierre no numérico
    [T0, 1.0, 2.0, 0.5, 0, 1.0],         # cierre cero
    [T0, 1.0, 2.0, 0.5, -3.0, 1.0],      # cierre negativo
    [T0],                                 # fila incompleta
    "no soy una lista",
])
def test_una_vela_inservible_se_salta_sin_romper_las_demas(malo):
    quotes = traer(Gecko(velas=[malo, vela(T0 - HORA, 7.0)]))
    assert [q.price for q in quotes] == [Decimal("7")]


# --- fallos de la API ---

def test_un_error_http_se_convierte_en_mensaje_claro():
    with pytest.raises(SolanaPriceError, match="HTTP 500"):
        traer(Gecko(status=500))


def test_un_fallo_de_red_se_convierte_en_mensaje_claro():
    with pytest.raises(SolanaPriceError):
        traer(Gecko(error=httpx.ConnectError("sin red")))


def test_un_token_sin_pools_se_dice_en_vez_de_devolver_vacio():
    """Devolver una lista vacía haría creer que el token no cotizó en ese periodo."""
    with pytest.raises(SolanaPriceError, match="sin pools"):
        traer(Gecko(pools=[]))


# --- precio actual (Jupiter) ---

class Jup:
    def __init__(self, precios=None, status=200, error=None):
        self.precios = precios or {}
        self.status = status
        self.error = error
        self.peticiones = []

    def __call__(self, request):
        self.peticiones.append(request)
        if self.error:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, text="no")
        return httpx.Response(200, json={
            m: {"usdPrice": p, "decimals": 5} for m, p in self.precios.items()})

    def source(self, **kw):
        return JupiterPriceSource(
            {"BONK/USDC": BONK, "WIF/USDC": WIF},
            client=httpx.Client(transport=httpx.MockTransport(self)),
            now=lambda: AHORA, **kw,
        )


def test_jupiter_da_el_precio_actual_de_un_token():
    q = Jup({BONK: 2.702427074091977e-06}).source().get_quote("BONK/USDC")
    assert q.price == Decimal("0.000002702427074091977")
    assert q.symbol == "BONK/USDC" and q.timestamp == AHORA


def test_un_simbolo_que_no_esta_en_el_mapa_es_imposible_de_valorar():
    """Permanente, no transitorio: sin mint no se podrá valorar nunca, y quien suma el
    capital total debe poder omitirlo en vez de abortar."""
    with pytest.raises(MarketUnpriceable):
        Jup().source().get_quote("LOQUESEA/USDC")


def test_un_token_que_jupiter_no_cotiza_es_imposible_de_valorar():
    with pytest.raises(MarketUnpriceable):
        Jup({WIF: 1.0}).source().get_quote("BONK/USDC")


@pytest.mark.parametrize("precio", [0, -1.0, None, "x", float("nan")])
def test_un_precio_no_utilizable_no_se_devuelve(precio):
    with pytest.raises(QuoteUnavailable):
        Jup({BONK: precio}).source().get_quote("BONK/USDC")


def test_un_fallo_de_red_es_transitorio_no_permanente():
    """Un corte de red NO puede confundirse con 'este token no vale nada': si se tratara
    como permanente, el capital total se calcularía omitiendo posiciones reales."""
    s = Jup(error=httpx.ConnectError("sin red")).source()
    with pytest.raises(QuoteUnavailable) as exc:
        s.get_quote("BONK/USDC")
    assert not isinstance(exc.value, MarketUnpriceable)


def test_varias_consultas_seguidas_no_machacan_la_api():
    """Un ciclo de salidas consulta todas las posiciones; sin caché serían N peticiones."""
    jup = Jup({BONK: 1.0, WIF: 2.0})
    s = jup.source()
    s.get_quote("BONK/USDC")
    s.get_quote("WIF/USDC")
    s.get_quote("BONK/USDC")
    assert len(jup.peticiones) == 1


def test_la_cache_caduca():
    jup = Jup({BONK: 1.0})
    reloj = [AHORA]
    s = JupiterPriceSource(
        {"BONK/USDC": BONK}, client=httpx.Client(transport=httpx.MockTransport(jup)),
        now=lambda: reloj[0], cache_seconds=10,
    )
    s.get_quote("BONK/USDC")
    reloj[0] = AHORA + timedelta(seconds=11)
    s.get_quote("BONK/USDC")
    assert len(jup.peticiones) == 2


# --- limite de tasa ---

class GeckoConFallos(Gecko):
    """Devuelve los codigos de `secuencia` antes de responder bien."""

    def __init__(self, secuencia, **kw):
        super().__init__(**kw)
        self.secuencia = list(secuencia)

    def __call__(self, request):
        if self.secuencia:
            self.peticiones.append(request)
            return httpx.Response(self.secuencia.pop(0), text="espera")
        return super().__call__(request)


def test_un_429_se_reintenta_en_vez_de_perder_la_descarga():
    """El plan gratuito limita a ~30 peticiones/minuto y bajar varios tokens lo roza. Si un
    429 abortara, media descarga se perderia por algo que se arregla esperando."""
    server = GeckoConFallos([429, 429], velas=[vela(T0, 4.0)])
    esperas = []
    quotes = traer(server, sleep=esperas.append)
    assert [q.price for q in quotes] == [Decimal("4")]
    assert len(esperas) >= 2 and esperas[0] > 0  # espero de verdad antes de reintentar


def test_un_5xx_tambien_se_reintenta():
    server = GeckoConFallos([503], velas=[vela(T0, 5.0)])
    assert [q.price for q in traer(server, sleep=lambda _s: None)] == [Decimal("5")]


def test_un_404_no_se_reintenta_porque_no_mejora_esperando():
    server = GeckoConFallos([404, 404, 404, 404, 404], velas=[vela(T0, 1.0)])
    with pytest.raises(SolanaPriceError, match="HTTP 404"):
        traer(server, sleep=lambda _s: None)
    assert len(server.peticiones) == 1  # una sola, no cinco


def test_si_el_limite_no_cede_se_dice_cuantos_intentos_hubo():
    server = GeckoConFallos([429] * 10, velas=[vela(T0, 1.0)])
    with pytest.raises(SolanaPriceError, match="4 intentos"):
        traer(server, sleep=lambda _s: None)
