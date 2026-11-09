"""Tests de los informes de solo lectura que contesta el bot por chat."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.models import ExitReason, Market, Mode, Position, Quote
from core.safety import activate_kill_switch, pause
from interfaces.reports import BotReports, parse_period
from ports.market import QuoteUnavailable
from storage.db import init_db
from storage.repository import Repository

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    return Repository(init_db(tmp_path / "bot.sqlite"))


@pytest.fixture
def make(tmp_path, repo):
    def _make(precios=None, mode=Mode.PAPER):
        def quote_fn(symbol):
            if precios is None or symbol not in precios:
                raise QuoteUnavailable(symbol)
            return Quote(symbol, Decimal(str(precios[symbol])), NOW)
        return BotReports(
            mode, tmp_path, lambda: repo,
            quote_fn=None if precios is None else quote_fn, now=lambda: NOW,
        )
    return _make


def pos(repo, pid="p1", symbol="BONK/USDC", entry="100", qty="2", opened=None):
    p = Position(
        id=pid, signal_id=f"s-{pid}", trader_id="t1", market=Market.SOLANA, symbol=symbol,
        entry_price=Decimal(entry), quantity=Decimal(qty),
        amount_usd=Decimal(entry) * Decimal(qty),
        opened_at=opened or NOW - timedelta(hours=2), peak_price=Decimal(entry),
    )
    repo.save_position(p)
    return p


# --- periodos ---

@pytest.mark.parametrize("texto,esperado", [
    ("7d", timedelta(days=7)), ("24h", timedelta(hours=24)),
    ("4w", timedelta(weeks=4)), ("1D", timedelta(days=1)), (None, timedelta(days=7)),
    ("", timedelta(days=7)),  # '/pnl ' con un espacio de mas es '/pnl'
])
def test_periodos_que_se_entienden(texto, esperado):
    assert parse_period(texto) == esperado


@pytest.mark.parametrize("texto", ["manana", "7", "d", "0d", "-3d", "99999d"])
def test_periodos_que_no_se_entienden(texto):
    assert parse_period(texto) is None


# --- STATUS ---

def test_status_dice_el_modo_siempre(make):
    assert "paper" in make().status()


def test_status_sin_candados_lo_dice(make):
    assert "ninguno" in make().status()


def test_status_muestra_los_candados_activos(make, tmp_path):
    pause(tmp_path, "x")
    activate_kill_switch(tmp_path, "y")
    texto = make().status()
    assert "KILL SWITCH" in texto and "EN PAUSA" in texto


def test_status_avisa_de_que_el_kill_switch_no_se_quita_por_chat(make, tmp_path):
    activate_kill_switch(tmp_path, "y")
    assert "terminal" in make().status()


def test_status_cuenta_las_posiciones_abiertas(make, repo):
    pos(repo, "p1")
    pos(repo, "p2")
    assert "posiciones abiertas: 2" in make().status()


def test_status_destaca_las_ordenes_sin_confirmar(make, repo):
    """Una orden sin confirmar es ambigua: pudo ejecutarse o no. No puede pasar desapercibida."""
    repo.reserve_intent("coid-1", {"symbol": "BONK/USDC"}, NOW)
    repo.mark_intent_sent("coid-1")
    assert "SIN CONFIRMAR" in make().status()


def test_status_suma_el_latente_cuando_hay_precios(make, repo):
    pos(repo, "p1", entry="100", qty="2")  # vale 240 a 120: +40
    assert "+40.00" in make(precios={"BONK/USDC": "120"}).status()


def test_status_sin_precios_no_inventa_un_latente(make, repo):
    pos(repo, "p1")
    assert "latente" not in make().status()


# --- POSITIONS ---

def test_sin_posiciones_lo_dice(make):
    assert "no hay posiciones" in make().positions()


def test_cada_posicion_lleva_porcentaje_y_resultado(make, repo):
    pos(repo, "p1", entry="100", qty="2")
    texto = make(precios={"BONK/USDC": "110"}).positions()
    assert "+10.00%" in texto and "+20.00" in texto


def test_una_posicion_sin_precio_lo_dice_y_no_tumba_el_informe(make, repo):
    """Falta el precio de una, pero la otra se calcula igual."""
    pos(repo, "p1", symbol="BONK/USDC", entry="100", qty="2")
    pos(repo, "p2", symbol="WIF/USDC", entry="50", qty="1")
    texto = make(precios={"WIF/USDC": "60"}).positions()
    assert "sin precio" in texto
    assert "+20.00%" in texto


def test_muchas_posiciones_se_recortan_para_caber_en_un_chat(make, repo):
    for i in range(25):
        pos(repo, f"p{i}", symbol=f"T{i}/USDC")
    texto = make().positions()
    assert "y 5 más" in texto


# --- PNL ---

def test_pnl_con_un_periodo_que_no_se_entiende_lo_dice(make):
    assert "no entiendo" in make().pnl("manana")


def test_pnl_sin_cierres_lo_dice(make):
    assert "ninguna posición cerrada" in make().pnl("7d")


def test_pnl_suma_los_cierres_del_periodo(make, repo):
    pos(repo, "p1", entry="100", qty="2")
    repo.close_position("p1", Decimal("110"), ExitReason.TAKE_PROFIT, NOW - timedelta(hours=1))
    pos(repo, "p2", entry="100", qty="1")
    repo.close_position("p2", Decimal("90"), ExitReason.STOP_LOSS, NOW - timedelta(hours=1))
    texto = make().pnl("7d")
    assert "+10.00" in texto           # +20 y -10
    assert "ganadoras: 1/2" in texto
    assert "take_profit: 1" in texto and "stop_loss: 1" in texto


def test_pnl_no_cuenta_lo_cerrado_fuera_del_periodo(make, repo):
    pos(repo, "p1", entry="100", qty="2", opened=NOW - timedelta(days=40))
    repo.close_position("p1", Decimal("200"), ExitReason.TAKE_PROFIT, NOW - timedelta(days=30))
    assert "ninguna posición cerrada" in make().pnl("7d")
