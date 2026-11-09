"""Tests del descubrimiento de wallets.

Este paso NO juzga: solo propone direcciones que la criba examinará con datos de Helius. Los
tests fijan sobre todo esa frontera, porque confundirla es como se llegó a copiar una wallet
de infraestructura creyendo que era un trader.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from adapters.solana.discover import (
    WalletLead,
    discover_wallets,
    fetch_pool_trades,
    fetch_top_pools,
    format_lead,
)
from adapters.solana.prices import SolanaPriceError

W1 = "eUwUeqKueB2kCUjmznww86te5cbTGqttAzD36BstWoJ"
W2 = "8KWeCnXGBmeuCFCoVr6p7hH6g3aDkowgc6oHYQ9smSCM"


def pool(pid, nombre, liquidez):
    return {"id": f"solana_{pid}", "attributes": {"name": nombre, "reserve_in_usd": liquidez}}


def trade(wallet, usd, kind="buy", anomalo=False, ts="2026-11-09T20:01:30Z"):
    return {"attributes": {
        "tx_from_address": wallet, "volume_in_usd": usd, "kind": kind,
        "is_anomalous": anomalo, "block_timestamp": ts,
    }}


def gecko(pools=(), trades_por_pool=None, trades_status=200):
    """Simula GeckoTerminal. OJO: la ruta de trades CONTIENE '/pools', asi que se comprueba
    primero; mirarlo al reves hace que el test pase por el camino equivocado."""
    trades_por_pool = trades_por_pool or {}

    def handler(request: httpx.Request) -> httpx.Response:
        ruta = request.url.path
        if "/trades" in ruta:
            if trades_status != 200:
                return httpx.Response(trades_status, json={})
            pid = ruta.split("/pools/")[1].split("/")[0]
            return httpx.Response(200, json={"data": list(trades_por_pool.get(pid, []))})
        return httpx.Response(200, json={"data": list(pools)})

    return httpx.Client(transport=httpx.MockTransport(handler))


def descubre(client, **kw):
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("min_pools", 1)
    return discover_wallets(client, **kw)


# --- pools ---

def test_se_queda_con_los_pools_con_liquidez():
    c = gecko([pool("a", "A/SOL", "100000"), pool("b", "B/SOL", "10")])
    assert [p[1] for p in fetch_top_pools(c, sleep=lambda _s: None)] == ["A/SOL"]


def test_los_pools_se_ordenan_por_liquidez():
    """Un pool pequeno mueve el precio con poco dinero: lo que parece un trader acertando
    puede ser alguien moviendo su propio pool."""
    c = gecko([pool("a", "A", "60000"), pool("b", "B", "900000")])
    assert [p[1] for p in fetch_top_pools(c, sleep=lambda _s: None)] == ["B", "A"]


def test_max_pools_limita_las_peticiones():
    c = gecko([pool(str(i), f"P{i}", "100000") for i in range(10)])
    assert len(fetch_top_pools(c, max_pools=3, sleep=lambda _s: None)) == 3


def test_sin_pools_con_liquidez_se_dice_como_arreglarlo():
    c = gecko([pool("a", "A", "10")])
    with pytest.raises(SolanaPriceError, match="min-liquidez"):
        descubre(c)


# --- intercambios ---

def test_se_descarta_el_polvo():
    c = gecko(trades_por_pool={"a": [trade(W1, "5"), trade(W2, "500")]})
    assert [t["wallet"] for t in fetch_pool_trades(c, "a", sleep=lambda _s: None)] == [W2]


def test_se_descarta_lo_marcado_como_anomalo():
    """GeckoTerminal marca asi los precios imposibles. Uno solo arruina una mediana."""
    c = gecko(trades_por_pool={"a": [trade(W1, "999999", anomalo=True), trade(W2, "500")]})
    assert [t["wallet"] for t in fetch_pool_trades(c, "a", sleep=lambda _s: None)] == [W2]


def test_un_intercambio_sin_wallet_no_revienta():
    c = gecko(trades_por_pool={"a": [{"attributes": {"volume_in_usd": "500"}}, trade(W1, "500")]})
    assert len(fetch_pool_trades(c, "a", sleep=lambda _s: None)) == 1


# --- agregacion ---

def test_agrupa_lo_que_hace_cada_wallet():
    c = gecko([pool("a", "A", "100000")],
              {"a": [trade(W1, "100"), trade(W1, "300", "sell"), trade(W2, "200")]})
    leads, _ = descubre(c)
    uno = next(l for l in leads if l.wallet == W1)
    assert uno.intercambios == 2 and uno.compras == 1 and uno.ventas == 1
    assert uno.mediana_usd == Decimal("200") and uno.mayor_usd == Decimal("300")


def test_min_pools_descarta_a_quien_solo_aparece_en_uno():
    """Aparecer en varios pools distingue a quien opera de quien paso por ahi una vez."""
    c = gecko([pool("a", "A", "100000"), pool("b", "B", "90000")],
              {"a": [trade(W1, "500"), trade(W2, "500")], "b": [trade(W1, "500")]})
    leads, _ = descubre(c, min_pools=2)
    assert [l.wallet for l in leads] == [W1]


def test_se_ordena_por_importe_mediano():
    c = gecko([pool("a", "A", "100000")], {"a": [trade(W1, "150"), trade(W2, "9000")]})
    leads, _ = descubre(c)
    assert leads[0].wallet == W2


def test_un_pool_que_falla_no_tumba_el_barrido():
    """Media lista perdida por un pool caido seria peor que una lista con un hueco."""
    def handler(request):
        if "/trades" in request.url.path:
            if "/pools/malo/" in request.url.path:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"data": [trade(W1, "500")]})
        return httpx.Response(200, json={"data": [pool("malo", "M", "100000"),
                                                  pool("bueno", "B", "90000")]})
    leads, recuento = descubre(httpx.Client(transport=httpx.MockTransport(handler)))
    assert [l.wallet for l in leads] == [W1]
    assert recuento["pools"] == 1 and recuento["pools_fallidos"] == 1


def test_se_cuenta_lo_recorrido():
    c = gecko([pool("a", "A", "100000")], {"a": [trade(W1, "500"), trade(W2, "500")]})
    _, recuento = descubre(c)
    assert recuento == {"pools": 1, "intercambios": 2, "pools_fallidos": 0}


def test_sin_intercambios_devuelve_una_lista_vacia_sin_romper():
    leads, _ = descubre(gecko([pool("a", "A", "100000")], {"a": []}))
    assert leads == []


# --- la frontera con la criba ---

def test_NO_se_calcula_ningun_ritmo_de_operaciones():
    """300 intercambios del pool mas activo pueden ser dos minutos. Un 'operaciones al dia'
    calculado sobre eso daria miles: un artefacto de la division, no una medida. Ese numero
    lo saca la criba leyendo el historial completo via Helius."""
    assert not hasattr(WalletLead, "por_dia")
    assert not hasattr(WalletLead, "dias")


def test_un_lead_no_dice_que_la_wallet_sirva():
    """Esto propone; la criba decide. Si WalletLead tuviera veredicto, habria dos opiniones
    distintas sobre la misma wallet y la mas barata ganaria por llegar antes."""
    assert not hasattr(WalletLead, "verdict")


def test_tokens_es_una_cota_inferior():
    lead = WalletLead(W1, 3, ("A", "B"), 2, 1, Decimal("100"), Decimal("300"))
    assert lead.tokens == 2


def test_el_texto_cabe_en_una_linea():
    texto = format_lead(WalletLead(W1, 3, ("A/SOL", "B/SOL"), 2, 1, Decimal("100"), Decimal("300")))
    assert "\n" not in texto and W1 in texto


# --- fechas ---

def test_se_guardan_la_primera_y_la_ultima_vez_que_se_le_vio():
    c = gecko([pool("a", "A", "100000")],
              {"a": [trade(W1, "100", ts="2026-11-09T20:00:00Z"),
                     trade(W1, "100", ts="2026-11-09T20:05:00Z")]})
    leads, _ = descubre(c)
    assert leads[0].primera == datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
    assert leads[0].ultima == datetime(2026, 11, 9, 20, 5, tzinfo=timezone.utc)


def test_una_fecha_ilegible_no_rompe_la_wallet():
    c = gecko([pool("a", "A", "100000")], {"a": [trade(W1, "100", ts="no-es-una-fecha")]})
    leads, _ = descubre(c)
    assert leads[0].primera is None and leads[0].intercambios == 1


# --- el historial entre barridos ---
# Un solo barrido mira minutos de los pools activos: selecciona bots por construccion, por
# mucho que se afinen los umbrales. Esto es lo que corrige ese sesgo.

from adapters.solana.discover import WalletHistory, format_history, merge_history

AYER = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
HOY = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def lead(wallet=W1, mediana="100", pools=("A",)):
    return WalletLead(wallet, 2, pools, 1, 1, Decimal(mediana), Decimal(mediana))


def test_el_primer_barrido_crea_la_entrada():
    h = merge_history([], [lead()], HOY)
    assert len(h) == 1 and h[0].barridos == 1
    assert h[0].primera == HOY and h[0].ultima == HOY


def test_volver_a_verla_suma_un_barrido():
    previos = merge_history([], [lead()], AYER)
    h = merge_history(previos, [lead()], HOY)
    assert h[0].barridos == 2 and h[0].primera == AYER and h[0].ultima == HOY


def test_el_contador_sube_UNA_vez_por_barrido_no_por_intercambio():
    """Contar intercambios devolveria el sesgo que esto corrige: lo que se mide es la
    persistencia en el tiempo, no la velocidad."""
    activa = WalletLead(W1, 500, ("A", "B"), 250, 250, Decimal("100"), Decimal("900"))
    assert merge_history([], [activa], HOY)[0].barridos == 1


def test_una_wallet_nueva_no_borra_las_anteriores():
    previos = merge_history([], [lead(W1)], AYER)
    h = merge_history(previos, [lead(W2)], HOY)
    assert {x.wallet for x in h} == {W1, W2}


def test_se_guarda_el_MAYOR_importe_mediano_visto():
    """Quedarse con el ultimo barrido borraria de que tamano es capaz de operar por un dia
    flojo cualquiera."""
    previos = merge_history([], [lead(mediana="5000")], AYER)
    h = merge_history(previos, [lead(mediana="80")], HOY)
    assert h[0].mediana_usd == Decimal("5000")


def test_los_pools_se_acumulan_sin_repetirse():
    previos = merge_history([], [lead(pools=("A", "B"))], AYER)
    h = merge_history(previos, [lead(pools=("B", "C"))], HOY)
    assert h[0].pools == ("A", "B", "C")


def test_se_ordena_por_numero_de_barridos():
    previos = merge_history([], [lead(W1), lead(W2, "9000")], AYER)
    h = merge_history(previos, [lead(W1)], HOY)
    assert h[0].wallet == W1, "2 barridos pesa mas que un importe grande en uno solo"


def test_los_dias_cubiertos_se_calculan_entre_el_primer_y_el_ultimo_barrido():
    previos = merge_history([], [lead()], AYER)
    assert merge_history(previos, [lead()], HOY)[0].dias == Decimal("1")


def test_un_solo_barrido_da_cero_dias_no_un_error():
    assert merge_history([], [lead()], HOY)[0].dias == Decimal("0")


def test_el_texto_del_historial_cabe_en_una_linea():
    h = WalletHistory(W1, 5, AYER, HOY, Decimal("100"), ("A",))
    assert "\n" not in format_history(h) and "5 barridos" in format_history(h)
