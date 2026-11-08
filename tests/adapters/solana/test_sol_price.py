"""Tests de la tabla de precios de SOL que permite valorar los swaps contra SOL.

El 81 % de los swaps de una wallet real de Solana no tocan ninguna stablecoin: van contra SOL.
Sin precio de SOL no se pueden valorar, y valorarlos con un precio viejo es peor que omitirlos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adapters.solana.sol_price import MAX_STALENESS, SolPriceError, SolPriceTable
from core.models import Quote


def ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 1, hour, minute, tzinfo=timezone.utc)


def q(hour: int, price: str, symbol: str = "SOL/USDT") -> Quote:
    """Cotización con sello de CIERRE de vela, como las que escribe fetch-quotes."""
    return Quote(symbol, Decimal(price), ts(hour))


def table(*quotes: Quote, **kw) -> SolPriceTable:
    return SolPriceTable(quotes, **kw)


# --- búsqueda sin mirar al futuro ---

def test_sello_exacto_devuelve_el_precio_de_esa_vela():
    t = table(q(9, "200"), q(10, "210"))
    assert t.price_at(ts(10)) == Decimal("210")


def test_entre_dos_velas_usa_la_anterior_nunca_la_siguiente():
    """Una vela sellada a las 10:00 no se conoce hasta las 10:00. Usarla a las 09:30 sería
    mirar al futuro: el swap se valoraría con un precio que aún no existía."""
    t = table(q(9, "200"), q(10, "999"))
    assert t.price_at(ts(9, 30)) == Decimal("200")


def test_antes_de_la_primera_vela_no_hay_precio():
    t = table(q(9, "200"))
    assert t.price_at(ts(8, 59)) is None


def test_las_cotizaciones_pueden_llegar_desordenadas():
    t = table(q(11, "220"), q(9, "200"), q(10, "210"))
    assert t.price_at(ts(10, 30)) == Decimal("210")


# --- el precio viejo no vale ---

def test_un_hueco_mayor_que_la_tolerancia_no_da_precio():
    """Un agujero en el histórico no se rellena con el último precio conocido: SOL se mueve un
    10 % en un día, así que valorar con un precio de ayer pone un tamaño falso en la señal."""
    t = table(q(9, "200"))
    assert t.price_at(ts(9) + MAX_STALENESS + timedelta(seconds=1)) is None


def test_justo_en_el_limite_de_la_tolerancia_sigue_valiendo():
    t = table(q(9, "200"))
    assert t.price_at(ts(9) + MAX_STALENESS) == Decimal("200")


def test_la_tolerancia_por_defecto_cubre_una_vela_horaria_ausente():
    """Con velas de 1h el retraso normal llega a 59 min (un swap a las 09:59 usa el cierre de
    las 09:00). La tolerancia debe pasar de una hora o se omitirían swaps perfectamente
    valorables, y cubrir dos para aguantar una vela que falte."""
    assert MAX_STALENESS >= timedelta(hours=2)


def test_la_tolerancia_es_configurable():
    t = table(q(9, "200"), max_staleness=timedelta(minutes=30))
    assert t.price_at(ts(9, 30)) == Decimal("200")
    assert t.price_at(ts(9, 31)) is None


# --- qué cotizaciones entran ---

def test_solo_entran_los_pares_de_sol():
    t = table(q(9, "200"), q(10, "0.00002", symbol="BONK/USDC"))
    assert t.price_at(ts(10)) == Decimal("200")  # el precio de BONK no es el de SOL
    assert len(t) == 1


def test_acepta_sol_contra_cualquier_stablecoin():
    assert len(table(q(9, "200", symbol="SOL/USDC"))) == 1
    assert len(table(q(9, "200", symbol="SOL/USDT"))) == 1


def test_sin_ninguna_cotizacion_de_sol_nunca_hay_precio():
    t = table(q(9, "0.00002", symbol="BONK/USDC"))
    assert len(t) == 0
    assert t.price_at(ts(9)) is None


def test_tabla_vacia_no_revienta():
    t = table()
    assert len(t) == 0
    assert t.price_at(ts(9)) is None


def test_con_sello_repetido_gana_el_ultimo_leido():
    t = table(q(9, "200"), q(9, "201"))
    assert t.price_at(ts(9)) == Decimal("201")


# --- lo que no se acepta ---

def test_un_precio_de_sol_no_positivo_es_error_no_un_cero_silencioso():
    """Un precio cero valoraría TODAS las señales contra SOL en 0 USD y el bot no compraría
    nada, sin decir por qué. Es un dato corrupto, no una señal sin valor."""
    with pytest.raises(SolPriceError, match="no positivo|positivo"):
        table(q(9, "0"))
    with pytest.raises(SolPriceError, match="no positivo|positivo"):
        table(q(9, "-200"))


def test_un_precio_no_finito_es_error():
    with pytest.raises(SolPriceError):
        table(Quote("SOL/USDT", Decimal("NaN"), ts(9)))


def test_una_fecha_sin_zona_horaria_es_error():
    """Comparar una fecha ingenua con una consciente lanza TypeError en mitad de la valoración;
    mejor rechazarla al construir la tabla, donde el mensaje dice qué pasa."""
    with pytest.raises(SolPriceError, match="zona horaria"):
        table(Quote("SOL/USDT", Decimal("200"), datetime(2026, 3, 1, 9)))


def test_consultar_con_una_fecha_sin_zona_horaria_es_error():
    t = table(q(9, "200"))
    with pytest.raises(SolPriceError, match="zona horaria"):
        t.price_at(datetime(2026, 3, 1, 9, 30))


def test_la_consulta_en_otra_zona_horaria_se_compara_en_utc():
    """Un sello con offset no es un sello distinto: 11:30+02:00 son las 09:30 UTC."""
    t = table(q(9, "200"), q(10, "210"))
    otra = datetime(2026, 3, 1, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    assert t.price_at(otra) == Decimal("200")
