"""Tests de los swaps valorados con el precio de SOL.

Contra Helius real, una wallet de Solana dio 545 swaps y CERO señales: 442 (el 81 %) se omitían
por `no_stable` porque iban token<->SOL, no contra USDC. La suposición "los traders operan
contra dólares" es falsa en Solana. Estos tests cubren valorar la pata de SOL con su precio.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from adapters.solana.source import parse_swap
from core.models import Market, Side

from tests.adapters.solana.test_source import BONK, USDC, WALLET, WIF, WSOL, leg, tx

SOL_200 = lambda _moment: Decimal("200")  # noqa: E731 - precio fijo para los tests
SIN_PRECIO = lambda _moment: None  # noqa: E731


def nat(amount: str, who: str = WALLET) -> dict:
    return {"account": who, "amount": amount}


def sol_buy(sig: str = "s1", lamports: str = "500000000", token: str = BONK) -> dict:
    """La wallet entrega SOL nativo y recibe un token: una compra."""
    return tx(sig, native_in=nat(lamports), outs=[leg(token, "5000000000", 5)])


def sol_sell(sig: str = "s1", lamports: str = "500000000", token: str = BONK) -> dict:
    """La wallet entrega un token y recibe SOL nativo: una venta."""
    return tx(sig, ins=[leg(token, "5000000000", 5)], native_out=nat(lamports))


# --- compra y venta contra SOL ---

def test_sol_a_token_es_una_compra_valorada_al_precio_de_sol():
    signal = parse_swap(sol_buy(), WALLET, sol_price=SOL_200)
    assert not isinstance(signal, str), signal
    assert signal.side is Side.BUY
    assert signal.trader_amount_usd == Decimal("100")  # 0,5 SOL x 200 USD
    assert signal.market is Market.SOLANA


def test_token_a_sol_es_una_venta():
    signal = parse_swap(sol_sell(), WALLET, sol_price=SOL_200)
    assert not isinstance(signal, str), signal
    assert signal.side is Side.SELL
    assert signal.trader_amount_usd == Decimal("100")


def test_el_wsol_envuelto_vale_igual_que_el_sol_nativo():
    """Jupiter unas veces deja la pata en SOL nativo y otras en wSOL. Es el mismo activo; si
    solo se valorase una, el mismo swap daría señal o no según por dónde lo enrutara."""
    envuelto = tx("s2", ins=[leg(WSOL, "500000000", 9)], outs=[leg(BONK, "5000000000", 5)])
    signal = parse_swap(envuelto, WALLET, sol_price=SOL_200)
    assert not isinstance(signal, str), signal
    assert signal.side is Side.BUY
    assert signal.trader_amount_usd == Decimal("100")


def test_el_simbolo_no_depende_de_por_donde_se_enrutara_el_swap():
    """La posición es en BONK pase por USDC o por SOL. Si una ruta diera BONK/SOL y la otra
    BONK/USDC, el mismo activo serían dos instrumentos distintos y la cartera se partiría."""
    mapa = {BONK: "BONK"}
    por_sol = parse_swap(sol_buy(), WALLET, mapa, sol_price=SOL_200)
    por_usdc = parse_swap(
        tx("s3", ins=[leg(USDC, "100000000", 6)], outs=[leg(BONK, "5000000000", 5)]), WALLET, mapa
    )
    assert por_sol.symbol == por_usdc.symbol == "BONK/USDC"


def test_el_importe_se_redondea_a_la_baja_a_ocho_decimales():
    """Un precio con muchos decimales daría un importe de 17 cifras. Se recorta a 8 y A LA BAJA:
    nunca se infla el tamaño que el trader movió realmente."""
    precio = lambda _m: Decimal("200.123456789")  # noqa: E731
    signal = parse_swap(sol_buy(), WALLET, sol_price=precio)
    assert signal.trader_amount_usd == Decimal("100.06172839")  # 100,0617283945 truncado


# --- sin precio no se inventa nada ---

def test_sin_precio_para_esa_hora_el_swap_se_omite_y_se_dice_por_que():
    assert parse_swap(sol_buy(), WALLET, sol_price=SIN_PRECIO) == "no_sol_price"


def test_sin_tabla_de_precios_se_sigue_omitiendo_como_antes():
    """Quien no pase precios de SOL mantiene el comportamiento anterior: nada se valora de más
    por el simple hecho de actualizar el lector."""
    assert parse_swap(sol_buy(), WALLET) == "no_stable"


def test_token_a_token_sigue_sin_poder_valorarse_aunque_haya_precio_de_sol():
    """El precio de SOL no dice nada de un swap BONK->WIF: ahí no hay ninguna pata conocida."""
    t2t = tx("s4", ins=[leg(BONK, "5000000000", 5)], outs=[leg(WIF, "7000000000", 6)])
    assert parse_swap(t2t, WALLET, sol_price=SOL_200) == "no_stable"


# --- las reglas anteriores siguen mandando ---

def test_el_polvo_de_sol_manda_sobre_la_valoracion():
    """0,005 SOL no es un swap: es la devolución del alquiler de una cuenta. Tener precio no lo
    convierte en una operación que copiar."""
    assert parse_swap(sol_buy(lamports="5000000"), WALLET, sol_price=SOL_200) == "below_dust"


def test_la_stablecoin_sigue_teniendo_prioridad_sobre_el_precio_estimado():
    """Si hay USDC en el swap, su importe se conoce EXACTO. Un precio de SOL es una estimación:
    no debe desplazar a un dato exacto."""
    con_ambas = tx(
        "s5", ins=[leg(USDC, "123456789", 6)], outs=[leg(BONK, "5000000000", 5)]
    )
    signal = parse_swap(con_ambas, WALLET, sol_price=SOL_200)
    assert signal.trader_amount_usd == Decimal("123.456789")


def test_varios_tokens_en_el_otro_lado_sigue_siendo_ambiguo():
    varios = tx(
        "s6", native_in=nat("500000000"),
        outs=[leg(BONK, "5000000000", 5), leg(WIF, "7000000000", 6)],
    )
    assert parse_swap(varios, WALLET, sol_price=SOL_200) == "ambiguous"


def test_una_wallet_que_no_participa_no_se_valora():
    ajeno = tx("s7", native_in=nat("500000000", who="otra"), outs=[leg(BONK, "5000000000", 5, who="otra")])
    assert parse_swap(ajeno, WALLET, sol_price=SOL_200) == "not_participant"


def test_una_transaccion_fallida_no_se_valora():
    fallida = tx("s8", native_in=nat("500000000"), outs=[leg(BONK, "5000000000", 5)], error={"err": 1})
    assert parse_swap(fallida, WALLET, sol_price=SOL_200) == "failed_tx"


# --- el id no puede depender del precio ---

def test_el_id_de_la_senal_no_cambia_con_el_precio():
    """El id alimenta el client_order_id que impide comprar dos veces la misma señal. Si
    dependiera del precio, bajar las cotizaciones otra vez daría otro id y el bot compraría
    dos veces el mismo swap."""
    caro = parse_swap(sol_buy(), WALLET, sol_price=lambda _m: Decimal("200"))
    barato = parse_swap(sol_buy(), WALLET, sol_price=lambda _m: Decimal("37.5"))
    assert caro.id == barato.id
    assert caro.trader_amount_usd != barato.trader_amount_usd


def test_el_id_es_el_mismo_se_valore_por_sol_o_por_stablecoin():
    """La misma firma es la misma señal: el id solo sale de la firma y de la wallet."""
    por_sol = parse_swap(sol_buy("misma"), WALLET, sol_price=SOL_200)
    por_usdc = parse_swap(
        tx("misma", ins=[leg(USDC, "100000000", 6)], outs=[leg(BONK, "5000000000", 5)]), WALLET
    )
    assert por_sol.id == por_usdc.id


# --- el precio se pide para el momento del swap ---

def test_el_precio_se_pide_con_la_hora_de_la_transaccion():
    """Pedirlo con la hora de la descarga valoraría un swap de hace un mes al precio de hoy."""
    pedidas: list[datetime] = []

    def espia(moment):
        pedidas.append(moment)
        return Decimal("200")

    signal = parse_swap(sol_buy(), WALLET, sol_price=espia)
    assert pedidas == [signal.timestamp]
    assert pedidas[0].tzinfo is timezone.utc


# --- el precio llega de verdad hasta el parser ---

def test_el_scan_de_una_wallet_valora_los_swaps_contra_sol():
    """Un `sol_price` que se perdiera entre la descarga y el parser no rompería ningún test de
    los de arriba: el scan seguiría omitiendo todo por `no_stable` sin decir nada."""
    from tests.adapters.solana.test_source import Helius, scan

    server = Helius([sol_buy("c1"), sol_sell("c2")])
    sin_precio, _ = scan(server)
    assert sin_precio.signals == [] and sin_precio.omitted == {"no_stable": 2}

    con_precio, _ = scan(Helius([sol_buy("c1"), sol_sell("c2")]), sol_price=SOL_200)
    assert con_precio.omitted == {}
    assert sorted(s.side for s in con_precio.signals) == sorted([Side.BUY, Side.SELL])
    assert all(s.trader_amount_usd == Decimal("100") for s in con_precio.signals)


def test_la_fuente_de_senales_valora_los_swaps_contra_sol():
    from tests.adapters.solana.test_source import KEY, Helius
    from adapters.solana.source import SolanaWalletSignalSource

    server = Helius([sol_buy("f1")])
    fuente = SolanaWalletSignalSource(
        [WALLET], KEY, client=server.client(), sol_price=SOL_200, sleep=lambda _s: None
    )
    señales = fuente.fetch_trades(None)
    assert [s.trader_amount_usd for s in señales] == [Decimal("100")]
    assert fuente.last_omitted == {}


def test_la_fuente_sin_precios_cuenta_el_motivo_de_la_omision():
    from tests.adapters.solana.test_source import KEY, Helius
    from adapters.solana.source import SolanaWalletSignalSource

    fuente = SolanaWalletSignalSource(
        [WALLET], KEY, client=Helius([sol_buy("f2")]).client(), sleep=lambda _s: None
    )
    assert fuente.fetch_trades(None) == []
    assert fuente.last_omitted == {"no_stable": 1}


def test_la_fuente_cuenta_los_swaps_sin_precio_para_esa_hora():
    from tests.adapters.solana.test_source import KEY, Helius
    from adapters.solana.source import SolanaWalletSignalSource

    fuente = SolanaWalletSignalSource(
        [WALLET], KEY, client=Helius([sol_buy("f3")]).client(),
        sol_price=SIN_PRECIO, sleep=lambda _s: None,
    )
    assert fuente.fetch_trades(None) == []
    assert fuente.last_omitted == {"no_sol_price": 1}
