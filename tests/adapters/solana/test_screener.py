"""Tests de la criba de wallets.

La wallet que se probó primero parecía un trader y era infraestructura. Estos tests fijan
las señales que la delataban, para que no vuelva a pasar desapercibida.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adapters.solana.screener import WalletProfile, format_profile, profile_wallet
from core.models import Market, Side, Signal

W = "8KWeCnXGBmeuCFCoVr6p7hH6g3aDkowgc6oHYQ9smSCM"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def sig(i: int, usd: str = "100", side=Side.BUY, symbol="BONK/USDC", horas: int = 0) -> Signal:
    return Signal(
        id=f"s{i}", trader_id=W, market=Market.SOLANA, symbol=symbol, side=side,
        timestamp=T0 + timedelta(hours=horas), trader_amount_usd=Decimal(usd),
    )


def muchas(n=12, usd="100"):
    """Un histórico sano: n operaciones, dos tokens, compras y ventas."""
    return [
        sig(i, usd,
            side=Side.BUY if i % 2 == 0 else Side.SELL,
            symbol="BONK/USDC" if i % 3 else "WIF/USDC",
            horas=i * 6)
        for i in range(n)
    ]


# --- lo que mide ---

def test_cuenta_compras_ventas_y_tokens():
    p = profile_wallet(W, muchas(), swaps_leidos=20)
    assert p.señales == 12 and p.compras == 6 and p.ventas == 6
    assert p.tokens == 2


def test_la_mediana_resiste_un_valor_extremo():
    """Una sola operación enorme no debe hacer parecer grande a un bot de micro-operaciones."""
    señales = [sig(i, "1") for i in range(9)] + [sig(99, "100000")]
    assert profile_wallet(W, señales, 10).mediana_usd == Decimal("1")


def test_mide_el_rango_de_tamaños():
    p = profile_wallet(W, [sig(1, "5"), sig(2, "50"), sig(3, "500")], 3)
    assert p.menor_usd == Decimal("5") and p.mayor_usd == Decimal("500")


def test_la_actividad_se_mide_sobre_el_periodo_real():
    señales = [sig(0, horas=0), sig(1, horas=24), sig(2, horas=48)]
    p = profile_wallet(W, señales, 3)
    assert p.dias == Decimal("2") and p.por_dia == Decimal("1.5")


def test_una_sola_operacion_no_da_una_actividad_infinita():
    """Dividir entre cero días daría 'infinitas operaciones al día'. Se devuelve None,
    que significa 'no se puede medir'; un 0 diría 'no opera', que es otra cosa."""
    p = profile_wallet(W, [sig(1)], 1)
    assert p.dias == Decimal(0) and p.por_dia is None


def test_sin_senales_no_revienta_nada():
    p = profile_wallet(W, [], swaps_leidos=0)
    assert p.mediana_usd == Decimal(0) and p.por_dia is None
    assert p.fee_payer_ratio == Decimal(0) and p.polvo_ratio == Decimal(0)


# --- lo que descarta ---

def test_una_wallet_sana_pasa_la_criba():
    sirve, motivos = profile_wallet(W, muchas(), 20).verdict()
    assert sirve and motivos == []


def test_se_descarta_a_quien_solo_paga_comisiones_ajenas():
    """El caso real: 77 de 545 swaps eran de otras cuentas y la wallet solo ponía la comisión."""
    p = profile_wallet(W, muchas(), swaps_leidos=100, omitidos={"not_participant": 50})
    sirve, motivos = p.verdict()
    assert not sirve
    assert any("infraestructura" in m for m in motivos)


def test_se_descarta_al_bot_de_micro_operaciones():
    """El caso real: operaciones de 1,31 a 6,03 USD, media 1,85."""
    p = profile_wallet(W, muchas(usd="1.85"), swaps_leidos=100)
    sirve, motivos = p.verdict()
    assert not sirve
    assert any("comisiones se comen" in m for m in motivos)


def test_se_descarta_a_quien_tiene_poco_historial():
    sirve, motivos = profile_wallet(W, muchas(n=3), 5).verdict()
    assert not sirve and any("historial" in m for m in motivos)


def test_se_descarta_a_quien_opera_un_solo_token():
    señales = [sig(i, side=Side.BUY if i % 2 else Side.SELL) for i in range(12)]
    sirve, motivos = profile_wallet(W, señales, 12).verdict()
    assert not sirve and any("un solo token" in m for m in motivos)


def test_se_descarta_a_quien_compra_y_nunca_vende():
    """Sin ventas no hay forma de saber si acierta: solo acumula."""
    señales = [sig(i, symbol=f"T{i % 3}/USDC") for i in range(12)]
    sirve, motivos = profile_wallet(W, señales, 12).verdict()
    assert not sirve and any("nunca vende" in m for m in motivos)


def test_se_acumulan_todos_los_motivos_no_solo_el_primero():
    """Saber que falla por tres razones distintas cambia la decisión frente a fallar por una."""
    p = profile_wallet(W, [sig(1, "0.5")], swaps_leidos=100, omitidos={"not_participant": 90})
    _, motivos = p.verdict()
    assert len(motivos) >= 3


# --- lo que NO promete ---

def test_pasar_la_criba_no_dice_que_gane_dinero():
    """La criba descarta; el backtest decide. Un trader excelente con posiciones de seis meses
    es inútil con un time-stop de 72 horas, y eso solo lo ve el backtest."""
    texto = format_profile(profile_wallet(W, muchas(), 20))
    assert "backtest" in texto


def test_el_retrato_explica_por_que_se_descarta():
    texto = format_profile(profile_wallet(W, muchas(usd="1"), 100, {"not_participant": 80}))
    assert "DESCARTADA" in texto
    assert "infraestructura" in texto and "comisiones se comen" in texto


def test_el_retrato_no_revienta_con_una_wallet_vacia():
    assert "DESCARTADA" in format_profile(profile_wallet(W, [], 0))


# --- una lectura truncada no certifica a nadie ---

def test_una_lectura_incompleta_impide_certificar():
    """Se leyeron los ultimos minutos de una wallet muy activa y se juzgo como si fuera toda
    su historia. Eso no es un veredicto conservador: es uno falso."""
    sirve, motivos = profile_wallet(W, muchas(), 500, completa=False).verdict()
    assert not sirve
    assert any("incompleta" in m for m in motivos)


def test_el_motivo_dice_como_arreglarlo():
    _, motivos = profile_wallet(W, muchas(), 500, completa=False).verdict()
    assert any("--max-requests" in m for m in motivos)


def test_una_lectura_completa_si_puede_certificar():
    sirve, _ = profile_wallet(W, muchas(), 20, completa=True).verdict()
    assert sirve


def test_una_ventana_diminuta_no_da_un_ritmo_absurdo():
    """Cuatro swaps en dos minutos dan '2.880 al dia'. Ese numero no describe a nadie."""
    señales = [sig(i, horas=0) for i in range(4)]
    p = profile_wallet(W, señales, 4)
    assert p.por_dia is None
    assert "demasiado corta" in format_profile(p)


def test_una_ventana_suficiente_si_da_el_ritmo():
    señales = [sig(0, horas=0), sig(1, horas=12), sig(2, horas=24)]
    p = profile_wallet(W, señales, 3)
    assert p.por_dia == Decimal("3")


# --- lo aprendido de los candidatos reales ---

def test_se_descarta_al_bot_de_alta_frecuencia():
    """Caso real: 1.417 operaciones en 2,4 horas. El ciclo de entrada corre cada hora, asi que
    solo veria unas pocas, elegidas por el azar de cuando toca el cron."""
    señales = [sig(i, symbol=f"T{i % 4}/USDC", side=Side.BUY if i % 2 else Side.SELL,
                   horas=i // 100) for i in range(600)]   # 600 ops en 6 horas
    sirve, motivos = profile_wallet(W, señales, 600).verdict()
    assert not sirve
    assert any("alta frecuencia" in m for m in motivos)


def test_se_descarta_a_quien_casi_solo_vende():
    """Caso real: 3 compras y 364 ventas. El bot copia ENTRADAS; ahi no hay nada que copiar."""
    señales = ([sig(i, symbol=f"T{i % 3}/USDC", side=Side.SELL, horas=i) for i in range(40)]
               + [sig(100, symbol="T0/USDC", side=Side.BUY, horas=41)])
    sirve, motivos = profile_wallet(W, señales, 41).verdict()
    assert not sirve
    assert any("copia ENTRADAS" in m for m in motivos)


def test_un_ritmo_humano_no_se_descarta():
    señales = [sig(i, symbol=f"T{i % 3}/USDC", side=Side.BUY if i % 2 else Side.SELL,
                   horas=i * 8) for i in range(12)]       # 12 ops en 88 horas
    sirve, motivos = profile_wallet(W, señales, 12).verdict()
    assert sirve, motivos


# --- enrutadores y arbitrajistas disfrazados de trader ---

def test_se_descarta_a_quien_casi_solo_hace_swaps_multi_pata():
    """Caso real: 16 operaciones limpias y 923 ambiguas en dos dias. La criba la certificaba
    como candidato porque solo miraba las 16. Las 923 decian lo que era: un enrutador."""
    p = profile_wallet(W, muchas(), swaps_leidos=1000, omitidos={"ambiguous": 950})
    sirve, motivos = p.verdict()
    assert not sirve
    assert any("enrutando o arbitrando" in m for m in motivos)


def test_un_trader_que_usa_agregadores_de_vez_en_cuando_NO_se_descarta():
    """Un agregador parte una orden en varias rutas, asi que algun swap ambiguo es normal.
    El umbral descarta a quien SOLO hace eso, no a quien lo hace a veces."""
    p = profile_wallet(W, muchas(), swaps_leidos=100, omitidos={"ambiguous": 40})
    sirve, motivos = p.verdict()
    assert sirve, motivos


def test_el_ratio_multi_pata_sale_en_el_retrato():
    texto = format_profile(profile_wallet(W, muchas(), 100, {"ambiguous": 50}))
    assert "multi-pata" in texto and "50%" in texto


def test_sin_swaps_leidos_el_ratio_es_cero_y_no_divide_entre_cero():
    assert profile_wallet(W, [], 0).ambiguo_ratio == Decimal(0)
