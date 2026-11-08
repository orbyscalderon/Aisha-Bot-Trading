"""Tests del comando fetch-solana-signals. Cliente y temporizador inyectados: sin red ni esperas."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from typer.testing import CliRunner

from adapters.cex.source import CexCsvSignalSource
from adapters.solana.source import HeliusError, load_token_map
from cli import CliDeps, app
from tests.adapters.solana.test_source import (
    BONK, KEY, T0, USDC, WALLET, WALLET2, WIF, Helius, buy, leg, sell, tx,
)
from tests.test_cli import write_yaml

NOW = datetime.fromtimestamp(T0 + 10_000, tz=timezone.utc)
SINCE = datetime.fromtimestamp(T0 - 1000, tz=timezone.utc).isoformat()


@pytest.fixture
def cfg_path(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


def run(cfg_path, tmp_path, txs_by_wallet, *extra, env=None, args=None):
    out = tmp_path / "out" / "signals.csv"
    servers = []

    def handler(request):
        wallet = request.url.path.split("/")[3]
        server = Helius(txs_by_wallet.get(wallet, []))
        servers.append(server)
        return server(request)

    deps = CliDeps(
        solana_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None, now=lambda: NOW,
    )
    argv = args if args is not None else [
        "fetch-solana-signals", "--config", str(cfg_path), "--root", str(tmp_path),
        "--wallet", WALLET, "--since", SINCE, "--out", str(out), *extra,
    ]
    r = CliRunner().invoke(app, argv, obj=deps, env={"HELIUS_API_KEY": KEY} if env is None else env)
    return r, out


def test_writes_a_csv_the_backtest_can_read(cfg_path, tmp_path):
    r, out = run(cfg_path, tmp_path, {WALLET: [sell("s2", T0 + 5), buy("s1", T0)]})
    assert r.exit_code == 0, r.output
    signals = CexCsvSignalSource(out).fetch_trades(None)
    assert [s.side.value for s in signals] == ["buy", "sell"]
    assert signals[0].trader_id == WALLET
    assert "2 señales" in r.output
    rows = out.read_text(encoding="utf-8").splitlines()
    assert rows[0].endswith(",signature") and rows[1].endswith(",s1") and rows[2].endswith(",s2")
    assert KEY not in r.output


def test_reports_how_many_swaps_were_omitted_and_why(cfg_path, tmp_path):
    odd = tx("o", ins=[leg(BONK, "1", 5)], outs=[leg(WIF, "1", 6)])
    odd2 = tx("o2", ins=[leg(WIF, "1", 6)], outs=[leg(BONK, "1", 5)])
    r, out = run(cfg_path, tmp_path, {WALLET: [buy("s1"), odd, odd2]})
    assert r.exit_code == 0, r.output
    assert "2 swaps omitidos" in r.output
    assert "no_stable: 2" in r.output
    assert "sin stablecoin" in r.output


def test_several_wallets_are_merged(cfg_path, tmp_path):
    other = tx("w2", T0 + 3, ins=[leg(USDC, "5000000", 6, who=WALLET2)], outs=[leg(BONK, "1", 5, who=WALLET2)])
    r, out = run(cfg_path, tmp_path, {WALLET: [buy("s1")], WALLET2: [other]}, "--wallet", WALLET2)
    assert r.exit_code == 0, r.output
    assert {s.trader_id for s in CexCsvSignalSource(out).fetch_trades(None)} == {WALLET, WALLET2}


def test_token_map_file_sets_symbols(cfg_path, tmp_path):
    tokens = tmp_path / "tokens.yaml"
    tokens.write_text(f"{BONK}: BONK\n", encoding="utf-8")
    r, out = run(cfg_path, tmp_path, {WALLET: [buy("s1")]}, "--token-map", str(tokens))
    assert r.exit_code == 0, r.output
    assert CexCsvSignalSource(out).fetch_trades(None)[0].symbol == "BONK/USDC"


def test_missing_api_key_is_a_message_with_code_2(cfg_path, tmp_path):
    r, out = run(cfg_path, tmp_path, {WALLET: [buy("s1")]}, env={})
    assert r.exit_code == 2 and "HELIUS_API_KEY" in r.output
    assert "Traceback" not in r.output and not out.exists()


def test_api_key_can_come_from_dotenv(cfg_path, tmp_path):
    (tmp_path / ".env").write_text(f"HELIUS_API_KEY={KEY}\n", encoding="utf-8")
    r, out = run(cfg_path, tmp_path, {WALLET: [buy("s1")]}, env={})
    assert r.exit_code == 0, r.output
    assert KEY not in r.output


@pytest.mark.parametrize("bad", ["no-es-una-wallet", "0OIl" * 9, ""])
def test_invalid_wallet_is_code_2(cfg_path, tmp_path, bad):
    out = tmp_path / "o.csv"
    r, _ = run(cfg_path, tmp_path, {}, args=[
        "fetch-solana-signals", "--config", str(cfg_path), "--root", str(tmp_path),
        "--wallet", bad, "--since", SINCE, "--out", str(out)])
    assert r.exit_code == 2 and "Traceback" not in r.output


def test_since_without_timezone_is_code_2(cfg_path, tmp_path):
    r, _ = run(cfg_path, tmp_path, {}, args=[
        "fetch-solana-signals", "--config", str(cfg_path), "--root", str(tmp_path),
        "--wallet", WALLET, "--since", "2026-11-01T00:00:00", "--out", str(tmp_path / "o.csv")])
    assert r.exit_code == 2 and "zona horaria" in r.output


def test_unexpected_response_shape_is_a_message_not_a_traceback(cfg_path, tmp_path):
    bad = buy("sigBAD")
    bad["timestamp"] = "ayer"
    r, out = run(cfg_path, tmp_path, {WALLET: [bad]})
    assert r.exit_code == 1 and "sigBAD" in r.output
    assert "Traceback" not in r.output and not out.exists()
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_no_swaps_writes_nothing_and_exits_1(cfg_path, tmp_path):
    r, out = run(cfg_path, tmp_path, {WALLET: []})
    assert r.exit_code == 1 and not out.exists()


def test_one_failing_wallet_gives_partial_exit_3_but_writes_the_rest(cfg_path, tmp_path):
    bad = buy("sigBAD")
    bad["timestamp"] = "ayer"
    r, out = run(cfg_path, tmp_path, {WALLET: [buy("s1")], WALLET2: [bad]}, "--wallet", WALLET2)
    assert r.exit_code == 3, r.output
    assert out.exists() and len(CexCsvSignalSource(out).fetch_trades(None)) == 1


def test_rejected_key_message_never_contains_the_key(cfg_path, tmp_path):
    deps = CliDeps(
        solana_client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401))),
        sleep=lambda s: None, now=lambda: NOW,
    )
    r = CliRunner().invoke(app, [
        "fetch-solana-signals", "--config", str(cfg_path), "--root", str(tmp_path),
        "--wallet", WALLET, "--since", SINCE, "--out", str(tmp_path / "o.csv"),
    ], obj=deps, env={"HELIUS_API_KEY": KEY})
    assert r.exit_code == 1 and "clave" in r.output
    assert KEY not in r.output


# --- mapeo mint -> símbolo ---

def test_load_token_map_reads_yaml_and_json(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(f"{BONK}: BONK\n{WIF}: WIF\n", encoding="utf-8")
    assert load_token_map(p) == {BONK: "BONK", WIF: "WIF"}
    j = tmp_path / "t.json"
    j.write_text(f'{{"{BONK}": "BONK"}}', encoding="utf-8")
    assert load_token_map(j) == {BONK: "BONK"}


@pytest.mark.parametrize("content", ["- a\n- b\n", f"{BONK}: 'A/B'\n", "no-mint: BONK\n", f"{BONK}: ''\n", ": : :\n"])
def test_load_token_map_rejects_bad_content(tmp_path, content):
    p = tmp_path / "t.yaml"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(HeliusError):
        load_token_map(p)


def test_load_token_map_missing_file(tmp_path):
    with pytest.raises(HeliusError):
        load_token_map(tmp_path / "nope.yaml")


def test_ignored_cursor_on_a_full_page_is_a_partial_read_with_code_3(cfg_path, tmp_path):
    # 100 swaps = pagina llena; el servidor ignora `before` y repite la misma pagina
    txs = [buy(f"s{i}", T0 + 1000 - i) for i in range(100)]
    deps_server = Helius(txs, page=100, ignore_before=True)
    deps = CliDeps(
        solana_client=httpx.Client(transport=httpx.MockTransport(deps_server)),
        sleep=lambda s: None, now=lambda: NOW,
    )
    out = tmp_path / "o.csv"
    r = CliRunner().invoke(app, [
        "fetch-solana-signals", "--config", str(cfg_path), "--root", str(tmp_path),
        "--wallet", WALLET, "--since", SINCE, "--out", str(out),
    ], obj=deps, env={"HELIUS_API_KEY": KEY})
    assert r.exit_code == 3, r.output
    assert "lectura incompleta" in r.output and "cursor" in r.output
    assert out.exists()
