"""Tests de `run --solana-wallet`: copiar wallets EN VIVO en vez de leer un CSV estático.

Sin esto, "el bot corre sin parar" significaba releer un fichero que nadie actualizaba.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from typer.testing import CliRunner

from cli import CliDeps, app
from tests.adapters.solana.test_source import BONK, KEY, T0, WALLET, leg, tx
from tests.test_cli import write_yaml

WSOL = "So11111111111111111111111111111111111111112"
AHORA = datetime.fromtimestamp(T0 + 600, tz=timezone.utc)


@pytest.fixture
def cfg_path(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


def swap_contra_sol(sig="v1", ts=T0):
    """La wallet entrega 0,5 SOL y recibe BONK: el caso normal en Solana."""
    return tx(sig, ts, native_in={"account": WALLET, "amount": "500000000"},
              outs=[leg(BONK, "5000000000", 5)])


def helius(txs):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("before"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=txs)
    return httpx.Client(transport=httpx.MockTransport(handler))


def jupiter(precios):
    def handler(request: httpx.Request) -> httpx.Response:
        pedidos = (request.url.params.get("ids") or "").split(",")
        return httpx.Response(200, json={
            m: {"usdPrice": p, "decimals": 5} for m, p in precios.items() if m in pedidos})
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(cfg_path, tmp_path, *extra, txs=(), precios=None, env=None, now=AHORA):
    deps = CliDeps(
        solana_client=helius(list(txs)),
        jupiter_client=jupiter(precios if precios is not None else {WSOL: 100.0, BONK: 0.00002}),
        sleep=lambda _s: None, now=lambda: now,
    )
    argv = ["run", "--config", str(cfg_path), "--root", str(tmp_path), "--once", *extra]
    return CliRunner().invoke(
        app, argv, obj=deps, env={"HELIUS_API_KEY": KEY} if env is None else env
    )


# --- qué fuente se usa ---

def test_sin_ninguna_fuente_se_dice_que_falta(cfg_path, tmp_path):
    r = run(cfg_path, tmp_path)
    assert r.exit_code == 2
    assert "--solana-wallet" in r.output and "--signals" in r.output


def test_las_dos_fuentes_a_la_vez_se_rechazan(cfg_path, tmp_path):
    """Dos cursores sobre la misma base y nadie sabría de dónde salió cada posición."""
    csv = tmp_path / "s.csv"
    csv.write_text("timestamp,trader_id,symbol,side,amount_usd\n", encoding="utf-8")
    r = run(cfg_path, tmp_path, "--signals", str(csv), "--solana-wallet", WALLET)
    assert r.exit_code == 2 and "no las dos a la vez" in r.output


# --- validación antes de tocar el disco ---

def test_una_wallet_mal_escrita_para_antes_de_crear_nada(cfg_path, tmp_path):
    r = run(cfg_path, tmp_path, "--solana-wallet", "esto-no-es-una-wallet")
    assert r.exit_code == 2 and "no válida" in r.output
    assert not (tmp_path / "paper.db").exists()


def test_sin_clave_de_helius_para_antes_de_crear_nada(cfg_path, tmp_path):
    r = run(cfg_path, tmp_path, "--solana-wallet", WALLET, env={})
    assert r.exit_code == 2 and "HELIUS_API_KEY" in r.output
    assert not (tmp_path / "paper.db").exists()


# --- el camino completo, sin red ---

def test_el_primer_ciclo_no_copia_historia(cfg_path, tmp_path):
    """Garantía deliberada del motor: sin cursor previo se pide desde AHORA. Arrancar el bot
    no puede dispararle un mes de operaciones viejas de golpe."""
    r = run(cfg_path, tmp_path, "--solana-wallet", WALLET, txs=[swap_contra_sol()])
    assert r.exit_code == 0, r.output
    assert "copiando EN VIVO 1 wallet(s)" in r.output
    assert "0 abiertas" in r.output


def test_un_swap_POSTERIOR_al_arranque_si_se_copia(cfg_path, tmp_path):
    """Helius -> señal valorada con el precio de SOL de Jupiter -> el motor la copia.
    Hacen falta dos ciclos: el primero fija el cursor, el segundo ve lo que pasó después."""
    primero = run(cfg_path, tmp_path, "--solana-wallet", WALLET)
    assert primero.exit_code == 0, primero.output

    despues = datetime.fromtimestamp(T0 + 1800, tz=timezone.utc)   # tras el primer ciclo
    segundo = run(
        cfg_path, tmp_path, "--solana-wallet", WALLET,
        txs=[swap_contra_sol("v9", ts=T0 + 1800)],
        now=despues + timedelta(minutes=5),
    )
    assert segundo.exit_code == 0, segundo.output
    assert "1 abiertas" in segundo.output


def test_sin_precio_de_sol_no_se_copia_nada(cfg_path, tmp_path):
    """Si Jupiter no da el precio de SOL, el swap se omite en vez de valorarse a ojo."""
    r = run(cfg_path, tmp_path, "--solana-wallet", WALLET,
            txs=[swap_contra_sol()], precios={BONK: 0.00002})
    assert r.exit_code == 0, r.output
    assert "0 abiertas" in r.output


def test_un_swap_demasiado_viejo_no_se_copia(cfg_path, tmp_path):
    """El precio actual de SOL no vale para valorar un swap de hace tres horas."""
    viejo = swap_contra_sol("v2", ts=T0 - 4 * 3600)
    r = run(cfg_path, tmp_path, "--solana-wallet", WALLET, txs=[viejo])
    assert r.exit_code == 0, r.output
    assert "0 abiertas" in r.output


def test_se_siguen_varias_wallets(cfg_path, tmp_path):
    from tests.adapters.solana.test_source import WALLET2
    r = run(cfg_path, tmp_path, "--solana-wallet", WALLET, "--solana-wallet", WALLET2)
    assert r.exit_code == 0, r.output
    assert "copiando EN VIVO 2 wallet(s)" in r.output


def test_una_wallet_repetida_cuenta_una_vez(cfg_path, tmp_path):
    r = run(cfg_path, tmp_path, "--solana-wallet", WALLET, "--solana-wallet", WALLET)
    assert r.exit_code == 0, r.output
    assert "copiando EN VIVO 1 wallet(s)" in r.output
