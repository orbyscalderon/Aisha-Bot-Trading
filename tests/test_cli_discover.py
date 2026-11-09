"""Tests de `discover-solana-wallets`.

El riesgo de este comando no es equivocarse midiendo: es que alguien lea su salida como un
veredicto. Por eso varios tests comprueban lo que DICE, no solo lo que calcula.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli import CliDeps, app
from tests.adapters.solana.test_discover import W1, W2, gecko, pool, trade
from tests.test_cli import write_yaml


@pytest.fixture
def cfg_path(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


def run(cfg_path, *extra, client=None, pools=(), trades=None):
    deps = CliDeps(
        gecko_client=client if client is not None else gecko(pools, trades or {}),
        sleep=lambda _s: None,
    )
    return CliRunner().invoke(
        app, ["discover-solana-wallets", "--config", str(cfg_path), "--min-pools", "1", *extra],
        obj=deps,
    )


UN_POOL = (pool("a", "A/SOL", "100000"),)


# --- el camino normal ---

def test_lista_las_direcciones_encontradas(cfg_path):
    r = run(cfg_path, pools=UN_POOL, trades={"a": [trade(W1, "500"), trade(W2, "900")]})
    assert r.exit_code == 0, r.output
    assert W1 in r.output and W2 in r.output


def test_cuenta_lo_que_ha_recorrido(cfg_path):
    r = run(cfg_path, pools=UN_POOL, trades={"a": [trade(W1, "500")]})
    assert "1 intercambios" in r.output and "1 pools" in r.output


def test_limit_recorta_la_lista_mostrada(cfg_path):
    trades = [trade(f"W{i}" + "x" * 40, str(100 * (i + 2))) for i in range(5)]
    r = run(cfg_path, "--limit", "2", pools=UN_POOL, trades={"a": trades})
    assert r.exit_code == 0, r.output
    assert sum(1 for l in r.output.splitlines() if "mediana" in l) == 2


def test_escribe_solo_las_direcciones_en_el_fichero(cfg_path, tmp_path):
    """El fichero esta hecho para encadenarlo con la criba. Guardar ahi lo que se mide aqui
    invitaria a decidir con unos numeros que no sirven para decidir."""
    destino = tmp_path / "salida" / "candidatas.txt"
    r = run(cfg_path, "--out", str(destino), pools=UN_POOL, trades={"a": [trade(W1, "500")]})
    assert r.exit_code == 0, r.output
    assert destino.read_text(encoding="utf-8").strip() == W1


# --- lo que NO promete ---

def test_avisa_de_que_ninguna_esta_aprobada(cfg_path):
    r = run(cfg_path, pools=UN_POOL, trades={"a": [trade(W1, "500")]})
    assert "NINGUNA esta aprobada" in r.output
    assert "screen-solana-wallets" in r.output


# --- cuando no hay nada ---

def test_sin_direcciones_dice_como_ampliar_la_busqueda(cfg_path):
    r = run(cfg_path, pools=UN_POOL, trades={"a": []})
    assert r.exit_code == 1
    assert "--min-operacion" in r.output and "--max-pools" in r.output


def test_sin_pools_con_liquidez_se_explica(cfg_path):
    r = run(cfg_path, pools=(pool("a", "A", "10"),))
    assert r.exit_code == 2 and "min-liquidez" in r.output


def test_un_pool_caido_no_tumba_el_barrido(cfg_path):
    def handler(request):
        if "/trades" in request.url.path:
            if "/pools/malo/" in request.url.path:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={"data": [trade(W1, "500")]})
        return httpx.Response(200, json={"data": [pool("malo", "M", "100000"),
                                                  pool("bueno", "B", "90000")]})
    r = run(cfg_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert r.exit_code == 0, r.output
    assert "1 no se pudieron leer" in r.output


# --- argumentos ---

def test_un_minimo_que_no_es_numero_se_rechaza(cfg_path):
    r = run(cfg_path, "--min-operacion", "mucho", pools=UN_POOL)
    assert r.exit_code == 2 and "numeros" in r.output


def test_un_minimo_negativo_se_rechaza(cfg_path):
    r = run(cfg_path, "--min-operacion", "-5", pools=UN_POOL)
    assert r.exit_code == 2 and "mayores que cero" in r.output


def test_no_hace_falta_clave_de_helius(cfg_path, tmp_path):
    """Descubrir es gratis a proposito: gastar peticiones de pago en direcciones que aun no
    sabes si valen es gastar justo donde menos informacion tienes."""
    r = run(cfg_path, pools=UN_POOL, trades={"a": [trade(W1, "500")]})
    assert r.exit_code == 0 and "HELIUS" not in r.output
