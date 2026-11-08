"""Tests del lector de wallets de Solana (Helius). Cliente httpx real sobre MockTransport:
ningún test abre sockets ni duerme en tiempo real."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from adapters.cex.source import CexCsvSignalSource
from adapters.solana.source import (
    HeliusError,
    SolanaWalletSignalSource,
    fetch_wallet_swaps,
    load_api_key,
    parse_swap,
    write_signals_csv,
)
from core.models import Market, Side

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
WSOL = "So11111111111111111111111111111111111111112"
WALLET2 = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
KEY = "SECRET-HELIUS-KEY-0123456789"
T0 = 1_790_000_000  # segundos unix


def leg(mint, raw, decimals, who=WALLET, **extra):
    return {"userAccount": who, "tokenAccount": "acc", "mint": mint,
            "rawTokenAmount": {"tokenAmount": raw, "decimals": decimals}, **extra}


def tx(sig, ts=T0, ins=(), outs=(), native_in=None, native_out=None, error=None, swap=True):
    event = {"nativeInput": native_in, "nativeOutput": native_out,
             "tokenInputs": list(ins), "tokenOutputs": list(outs)}
    return {"signature": sig, "timestamp": ts, "type": "SWAP", "transactionError": error,
            "events": {"swap": event} if swap else {}}


def buy(sig, ts=T0, usdc="250000000", token=BONK):
    return tx(sig, ts, ins=[leg(USDC, usdc, 6)], outs=[leg(token, "5000000000", 5)])


def sell(sig, ts=T0, usdt="100500000"):
    return tx(sig, ts, ins=[leg(BONK, "5000000000", 5)], outs=[leg(USDT, usdt, 6)])


class Helius:
    """Servidor falso: sirve la lista `txs` (más nueva primero) paginando por `before`."""

    def __init__(self, txs, page=2, ignore_before=False, statuses=()):
        self.txs, self.page, self.ignore_before = txs, page, ignore_before
        self.statuses = list(statuses)  # códigos (o excepciones) a devolver antes de responder bien
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        if self.statuses:
            code = self.statuses.pop(0)
            if isinstance(code, Exception):
                raise code
            return httpx.Response(code, text="boom")
        before = request.url.params.get("before")
        start = 0
        if before and not self.ignore_before:
            sigs = [t["signature"] for t in self.txs]
            start = sigs.index(before) + 1
        return httpx.Response(200, json=self.txs[start:start + self.page])

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def scan(server, **kw):
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    result = fetch_wallet_swaps(server.client(), WALLET, KEY, **kw)
    return result, sleeps


# --- qué es compra y qué es venta ---

def test_stable_to_token_is_a_buy_valued_in_the_stable():
    s = parse_swap(buy("sigA"), WALLET)
    assert s.side is Side.BUY and s.market is Market.SOLANA
    assert s.trader_amount_usd == Decimal("250")
    assert s.symbol == f"{BONK}/USDC"  # sin símbolo conocido: la dirección del mint
    assert s.trader_id == WALLET
    assert s.timestamp == datetime.fromtimestamp(T0, tz=timezone.utc)


def test_token_to_stable_is_a_sell():
    s = parse_swap(sell("sigB"), WALLET)
    assert s.side is Side.SELL and s.symbol == f"{BONK}/USDT"
    assert s.trader_amount_usd == Decimal("100.5")


def test_amount_uses_decimals_exactly_and_accepts_numeric_json():
    assert parse_swap(buy("s", usdc="250123456"), WALLET).trader_amount_usd == Decimal("250.123456")
    # un int de JSON se convierte por str(), nunca Decimal(float)
    assert parse_swap(buy("s", usdc=250000000), WALLET).trader_amount_usd == Decimal("250")
    t = tx("s", ins=[leg(USDC, "1", 6), leg(USDC, "2", 6)], outs=[leg(BONK, "1", 5)])
    assert parse_swap(t, WALLET).trader_amount_usd == Decimal("0.000003")


def test_token_map_beats_helius_symbol_and_both_beat_the_mint():
    t = tx("s", ins=[leg(USDC, "1000000", 6)], outs=[leg(BONK, "1", 5, symbol="XYZ")])
    assert parse_swap(t, WALLET).symbol == "XYZ/USDC"
    assert parse_swap(t, WALLET, {BONK: "BONK"}).symbol == "BONK/USDC"


@pytest.mark.parametrize("bad", ["", "  ", "A/B", "a,b", "x" * 40, 5, None, "emoji☃"])
def test_unusable_helius_symbol_falls_back_to_mint(bad):
    t = tx("s", ins=[leg(USDC, "1000000", 6)], outs=[leg(BONK, "1", 5, symbol=bad)])
    assert parse_swap(t, WALLET).symbol == f"{BONK}/USDC"


@pytest.mark.parametrize(
    "transaction, reason",
    [
        (tx("s", ins=[leg(BONK, "1", 5)], outs=[leg(WIF, "1", 6)]), "no_stable"),
        (tx("s", native_in={"account": WALLET, "amount": "1000000000"}, outs=[leg(BONK, "1", 5)]), "no_stable"),
        (tx("s", ins=[leg(USDC, "1000000", 6)], outs=[leg(USDT, "1000000", 6)]), "stable_to_stable"),
        (tx("s", ins=[leg(USDC, "1000000", 6)], outs=[leg(BONK, "1", 5), leg(WIF, "1", 6)]), "ambiguous"),
        (tx("s", ins=[leg(USDC, "1000000", 6), leg(BONK, "1", 5)], outs=[leg(WIF, "1", 6)]), "ambiguous"),
        (tx("s", ins=[leg(USDC, "1000000", 6), leg(USDT, "1000000", 6)], outs=[leg(BONK, "1", 5)]), "ambiguous"),
        (tx("s", ins=[leg(USDC, "1000000", 6)], outs=[leg(BONK, "1", 5), leg(USDT, "5", 6)]), "ambiguous"),
        (tx("s", ins=[leg(USDC, "0", 6)], outs=[leg(BONK, "1", 5)]), "zero_amount"),
        (buy("s") | {"transactionError": {"InstructionError": [0, "Custom"]}}, "failed_tx"),
        (tx("s", swap=False), "no_swap_event"),
        (tx("s", ins=[leg(USDC, "1000000", 6, who="otra")], outs=[leg(BONK, "1", 5, who="otra")]), "not_participant"),
    ],
)
def test_unvaluable_or_ambiguous_swaps_are_omitted_with_a_reason(transaction, reason):
    assert parse_swap(transaction, WALLET) == reason


def test_only_the_followed_wallets_legs_count():
    # la contraparte del swap aparece en otras patas: no deben contaminar el cálculo
    t = tx("s", ins=[leg(USDC, "250000000", 6), leg(WIF, "9", 6, who="mm")],
           outs=[leg(BONK, "5", 5), leg(USDC, "9", 6, who="mm")])
    s = parse_swap(t, WALLET)
    assert s.side is Side.BUY and s.trader_amount_usd == Decimal("250")


# --- forma inesperada: error claro, nunca datos mal ---

@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.pop("signature"),
        lambda t: t.update(signature=""),
        lambda t: t.pop("timestamp"),
        lambda t: t.update(timestamp="2026-01-01"),
        lambda t: t.update(timestamp=True),
        lambda t: t["events"].update(swap=[]),
        lambda t: t["events"]["swap"].update(tokenInputs={"a": 1}),
        lambda t: t["events"]["swap"]["tokenInputs"][0].pop("mint"),
        lambda t: t["events"]["swap"]["tokenInputs"][0].pop("rawTokenAmount"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].pop("decimals"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(decimals="6"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount="abc"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount="-5"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount=None),
        lambda t: t.update(events=5),
        # unidades minimas = enteros: lo demas delata que Helius manda unidades de interfaz
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount="250.5"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount="1e8"),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount=250.0),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount=1e8),
        lambda t: t["events"]["swap"]["tokenInputs"][0]["rawTokenAmount"].update(tokenAmount=" 5"),
        lambda t: t["events"]["swap"].update(nativeOutput={"account": WALLET, "amount": "0.5"}),
        lambda t: t["events"]["swap"].update(nativeOutput={"account": WALLET}),
        lambda t: t["events"]["swap"].update(nativeOutput="x"),
    ],
)
def test_unexpected_shape_raises_a_clear_error(mutate):
    t = buy("sigX")
    mutate(t)
    with pytest.raises(HeliusError) as exc:
        parse_swap(t, WALLET)
    assert str(exc.value)


def test_integrality_error_names_the_problem():
    t = buy("sigX", usdc="250.5")
    with pytest.raises(HeliusError, match="entero"):
        parse_swap(t, WALLET)


# --- pata de SOL nativo: polvo ignorado, swaps reales con SOL valorados ---

DUST = {"account": WALLET, "amount": "2039280"}  # alquiler de una cuenta de tokens: 0,00203928 SOL


def test_usdc_to_token_with_native_dust_output_is_still_a_signal():
    # el caso real de Jupiter: el cierre de la cuenta wSOL devuelve alquiler en nativeOutput
    t = buy("s")
    t["events"]["swap"]["nativeOutput"] = DUST
    s = parse_swap(t, WALLET)
    assert not isinstance(s, str), s
    assert s.side is Side.BUY and s.symbol == f"{BONK}/USDC" and s.trader_amount_usd == Decimal("250")


def test_token_to_stable_with_native_dust_input_is_still_a_signal():
    t = sell("s")
    t["events"]["swap"]["nativeInput"] = DUST
    assert parse_swap(t, WALLET).side is Side.SELL


def test_wsol_token_leg_dust_is_ignored_too():
    t = buy("s")
    t["events"]["swap"]["tokenOutputs"].append(leg(WSOL, "2039280", 9))
    assert parse_swap(t, WALLET).symbol == f"{BONK}/USDC"


def test_dust_threshold_boundary():
    just_under = buy("s")
    just_under["events"]["swap"]["nativeOutput"] = {"account": WALLET, "amount": "9999999"}
    assert not isinstance(parse_swap(just_under, WALLET), str)
    at_threshold = buy("s")
    at_threshold["events"]["swap"]["nativeOutput"] = {"account": WALLET, "amount": "10000000"}
    assert parse_swap(at_threshold, WALLET) == "ambiguous"  # 0,01 SOL ya es un token de verdad


def test_usdc_to_sol_is_a_buy_of_SOL_not_of_the_wsol_mint():
    t = tx("s", ins=[leg(USDC, "150000000", 6)], native_out={"account": WALLET, "amount": "1000000000"})
    s = parse_swap(t, WALLET)
    assert s.symbol == "SOL/USDC" and s.side is Side.BUY and s.trader_amount_usd == Decimal("150")


def test_sol_to_usdt_is_a_sell_of_SOL_and_wsol_leg_counts_like_native():
    t = tx("s", ins=[leg(WSOL, "2000000000", 9)], outs=[leg(USDT, "300000000", 6)])
    s = parse_swap(t, WALLET)
    assert s.symbol == "SOL/USDT" and s.side is Side.SELL
    t2 = tx("s", native_in={"account": WALLET, "amount": "2000000000"}, outs=[leg(USDT, "300000000", 6)])
    assert parse_swap(t2, WALLET).symbol == "SOL/USDT"


def test_native_leg_of_someone_else_is_ignored():
    t = buy("s")
    t["events"]["swap"]["nativeOutput"] = {"account": "otra", "amount": "5000000000"}
    assert not isinstance(parse_swap(t, WALLET), str)


def test_token_map_can_override_the_default_sol_symbol():
    t = tx("s", ins=[leg(USDC, "150000000", 6)], native_out={"account": WALLET, "amount": "1000000000"})
    assert parse_swap(t, WALLET, {WSOL: "WSOL"}).symbol == "WSOL/USDC"


def test_non_dict_transaction_is_an_error():
    with pytest.raises(HeliusError):
        parse_swap("hola", WALLET)


@pytest.mark.parametrize("body", [{"error": "bad key"}, "texto", None, 5])
def test_response_that_is_not_a_list_is_an_error(body):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    with pytest.raises(HeliusError, match="respuesta"):
        fetch_wallet_swaps(httpx.Client(transport=transport), WALLET, KEY, sleep=lambda s: None)


def test_non_json_response_is_an_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(HeliusError, match="JSON"):
        fetch_wallet_swaps(httpx.Client(transport=transport), WALLET, KEY, sleep=lambda s: None)


def test_a_malformed_transaction_aborts_the_scan_with_context():
    bad = buy("sigBAD")
    bad["timestamp"] = "ayer"
    server = Helius([buy("sig1"), bad])
    with pytest.raises(HeliusError, match="sigBAD"):
        scan(server)


# --- id estable ---

def test_signal_id_is_stable_and_derived_from_the_signature():
    a1 = parse_swap(buy("sigA"), WALLET)
    a2 = parse_swap(buy("sigA", usdc="999000000", ts=T0 + 5), WALLET)  # solo cambia lo demás
    assert a1.id == a2.id
    assert parse_swap(buy("sigB"), WALLET).id != a1.id
    # misma firma, otra wallet seguida: es otra señal
    other = tx("sigA", ins=[leg(USDC, "1", 6, who=WALLET2)], outs=[leg(BONK, "1", 5, who=WALLET2)])
    assert parse_swap(other, WALLET2).id != a1.id
    assert len(a1.id) == 32 and a1.id.isalnum()


def test_ids_survive_re_reading():
    txs = [buy("s1"), sell("s2"), buy("s3")]
    first = [s.id for s in scan(Helius(txs))[0].signals]
    second = [s.id for s in scan(Helius(txs))[0].signals]
    assert first == second and len(set(first)) == 3


# --- paginación ---

def test_request_shape_and_pagination_by_last_signature():
    server = Helius([buy("s1", T0 + 4), buy("s2", T0 + 3), buy("s3", T0 + 2), buy("s4", T0 + 1)])
    result, sleeps = scan(server)
    assert len(result.signals) == 4
    first = server.requests[0]
    assert first.url.path == f"/v0/addresses/{WALLET}/transactions"
    assert first.url.host == "api.helius.xyz"
    assert first.url.params["type"] == "SWAP" and first.url.params["api-key"] == KEY
    assert "before" not in first.url.params
    assert server.requests[1].url.params["before"] == "s2"
    assert server.requests[2].url.params["before"] == "s4"
    assert result.stop == "empty"
    assert result.complete
    assert len(sleeps) == len(server.requests) - 1  # pausa entre páginas, no tras la última


def test_stops_when_the_cursor_does_not_advance():
    # un servidor que ignora `before` devolvería siempre lo mismo: no se pide eternamente
    server = Helius([buy("s1"), buy("s2")], ignore_before=True)
    result, _ = scan(server)
    assert result.stop == "no_advance"
    assert len(server.requests) == 2
    assert len(result.signals) == 2  # sin duplicados


def test_hard_cap_on_requests_marks_the_scan_incomplete():
    server = Helius([buy(f"s{i}", T0 + 100 - i) for i in range(50)], page=1)
    result, _ = scan(server, max_requests=5)
    assert len(server.requests) == 5
    assert result.stop == "max_requests" and not result.complete
    assert len(result.signals) == 5


def test_stops_once_a_page_reaches_before_since():
    server = Helius([buy("s1", T0 + 30), buy("s2", T0 + 20), buy("s3", T0 + 10), buy("s4", T0)], page=2)
    result, _ = scan(server, since=datetime.fromtimestamp(T0 + 15, tz=timezone.utc))
    assert len(result.signals) == 2  # s3 y s4 son anteriores a since
    assert len(server.requests) == 2
    assert result.stop == "since"


def test_empty_first_page_is_a_clean_empty_result():
    result, _ = scan(Helius([]))
    assert result.signals == [] and result.stop == "empty" and result.complete


def test_omissions_are_counted_by_reason():
    txs = [buy("a"), tx("b", ins=[leg(BONK, "1", 5)], outs=[leg(WIF, "1", 6)]),
           tx("c", ins=[leg(WIF, "1", 6)], outs=[leg(BONK, "1", 5)]), sell("d"), tx("e", swap=False)]
    result, _ = scan(Helius(txs, page=10))
    assert len(result.signals) == 2
    assert result.omitted == {"no_stable": 2, "no_swap_event": 1}


# --- reintentos y errores HTTP ---

def test_rate_limit_is_retried_with_backoff_and_counts_against_the_cap():
    server = Helius([buy("s1")], page=5, statuses=[429, 503])
    result, sleeps = scan(server)
    assert len(result.signals) == 1
    assert sleeps[:2] == [1.0, 2.0]
    assert result.requests == len(server.requests) == 4  # 429, 503, ok, vacía


def test_endless_rate_limit_gives_up_with_a_clear_error():
    server = Helius([buy("s1")], statuses=[429] * 50)
    with pytest.raises(HeliusError, match="429") as exc:
        scan(server)
    assert len(server.requests) <= 6 and KEY not in str(exc.value)


def test_retry_attempts_cannot_exceed_max_requests():
    server = Helius([], statuses=[500] * 50)
    with pytest.raises(HeliusError):
        scan(server, max_requests=2)
    assert len(server.requests) == 2


def test_transport_error_is_retried_and_its_message_hides_the_key():
    boom = httpx.ConnectError(f"no conecta con https://api.helius.xyz/x?api-key={KEY}")
    server = Helius([buy("s1")], statuses=[boom] * 50)
    with pytest.raises(HeliusError) as exc:
        scan(server)
    assert KEY not in str(exc.value) and len(server.requests) > 1


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_fails_at_once_without_retry_and_without_the_key(status):
    server = Helius([], statuses=[status])
    with pytest.raises(HeliusError, match="clave") as exc:
        scan(server)
    assert len(server.requests) == 1 and KEY not in str(exc.value)


def test_other_4xx_is_an_error_with_the_status_only():
    server = Helius([], statuses=[400])
    with pytest.raises(HeliusError, match="400") as exc:
        scan(server)
    assert KEY not in str(exc.value)


# --- la clave de API no se filtra a los logs ---

@pytest.mark.parametrize("statuses", [[], [429], [401]])
def test_api_key_not_leaked_by_httpx_own_logger(caplog, statuses):
    server = Helius([buy("s1")], statuses=statuses)
    with caplog.at_level(logging.INFO):
        try:
            scan(server)
        except HeliusError:
            pass
    assert any(r.name == "httpx" for r in caplog.records), "httpx debe seguir registrando"
    assert any("api-key=***" in r.getMessage() for r in caplog.records if r.name == "httpx")
    assert KEY not in caplog.text
    assert "SECRET-HELIUS" not in caplog.text


def test_api_key_not_leaked_at_debug_level_either(caplog):
    with caplog.at_level(logging.DEBUG):
        scan(Helius([buy("s1")]))
    assert KEY not in caplog.text


# --- fuente de señales (puerto) ---

def source(txs_by_wallet, **kw):
    def handler(request):
        wallet = request.url.path.split("/")[3]
        return Helius(txs_by_wallet[wallet])(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SolanaWalletSignalSource([WALLET, WALLET2], KEY, client=client, sleep=lambda s: None, **kw)


def test_source_merges_wallets_sorted_and_filters_strictly_after_since():
    src = source({WALLET: [buy("w1", T0 + 20), buy("w1b", T0 + 5)], WALLET2: [tx("w2", T0 + 10, ins=[leg(BONK, "5", 5, who=WALLET2)], outs=[leg(USDT, "7000000", 6, who=WALLET2)])]})
    signals = src.fetch_trades(None)
    assert [s.timestamp.timestamp() for s in signals] == [T0 + 5, T0 + 10, T0 + 20]
    assert {s.trader_id for s in signals} == {WALLET, WALLET2}
    later = src.fetch_trades(datetime.fromtimestamp(T0 + 10, tz=timezone.utc))
    assert [s.timestamp.timestamp() for s in later] == [T0 + 20]
    assert src.list_followed() == sorted([WALLET, WALLET2])
    assert src.name == "solana"


def test_source_accumulates_omissions_and_completeness():
    odd = tx("o", ins=[leg(BONK, "1", 5)], outs=[leg(WIF, "1", 6)])
    src = source({WALLET: [buy("a"), odd], WALLET2: [tx("o2", ins=[leg(BONK, "1", 5, who=WALLET2)], outs=[leg(WIF, "1", 6, who=WALLET2)])]})
    src.fetch_trades(None)
    assert src.last_omitted == {"no_stable": 2}
    assert src.last_complete


# --- CSV ---

def test_csv_is_readable_by_the_backtest_reader(tmp_path):
    signals = [parse_swap(buy("a", T0), WALLET), parse_swap(sell("b", T0 + 60), WALLET)]
    out = tmp_path / "sub" / "signals.csv"
    write_signals_csv(out, signals)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "timestamp,trader_id,symbol,side,amount_usd,signature"
    assert lines[1].split(",")[1:] == [WALLET, f"{BONK}/USDC", "buy", "250", ""]
    assert lines[1].startswith(datetime.fromtimestamp(T0, tz=timezone.utc).isoformat())
    back = CexCsvSignalSource(out).fetch_trades(None)
    assert [(s.side, s.trader_amount_usd, s.symbol) for s in back] == [
        (Side.BUY, Decimal("250"), f"{BONK}/USDC"), (Side.SELL, Decimal("100.5"), f"{BONK}/USDT")]


def test_csv_write_failure_is_a_helius_error(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    with pytest.raises(HeliusError):
        write_signals_csv(blocker / "out.csv", [parse_swap(buy("a"), WALLET)])


# --- la clave vive en el entorno / .env ---

def test_api_key_from_environment_wins_over_dotenv(tmp_path):
    (tmp_path / ".env").write_text("HELIUS_API_KEY=de-fichero\n", encoding="utf-8")
    assert load_api_key({"HELIUS_API_KEY": "de-entorno"}, tmp_path) == "de-entorno"


@pytest.mark.parametrize(
    "content",
    ["HELIUS_API_KEY=abc\n", 'HELIUS_API_KEY="abc"\n', "# c\nOTRA=1\nexport HELIUS_API_KEY='abc'  \n",
     "﻿HELIUS_API_KEY=abc\r\n"],
)
def test_api_key_from_dotenv_file(tmp_path, content):
    (tmp_path / ".env").write_bytes(content.encode("utf-8"))
    assert load_api_key({}, tmp_path) == "abc"


@pytest.mark.parametrize("content", [None, "HELIUS_API_KEY=\n", "OTRA=1\n", "# HELIUS_API_KEY=abc\n"])
def test_missing_or_empty_api_key_is_a_clear_error(tmp_path, content):
    if content is not None:
        (tmp_path / ".env").write_text(content, encoding="utf-8")
    with pytest.raises(HeliusError, match="HELIUS_API_KEY"):
        load_api_key({}, tmp_path)


# --- orden de las paginas ---

def test_page_not_newest_first_is_an_error_instead_of_a_silent_truncation():
    server = Helius([buy("s1", T0), buy("s2", T0 + 10)], page=5)  # orden invertido
    with pytest.raises(HeliusError, match="orden"):
        scan(server, since=datetime.fromtimestamp(T0 - 5, tz=timezone.utc))


def test_equal_timestamps_are_fine():
    result, _ = scan(Helius([buy("s1", T0), buy("s2", T0), buy("s3", T0 - 1)], page=5))
    assert len(result.signals) == 3


# --- la limpieza de logs cubre tambien a los loggers hijos ---

@pytest.mark.parametrize("name", ["httpx", "httpcore", "httpcore.http11", "httpcore.connection", "httpx._client"])
def test_api_key_is_scrubbed_from_child_loggers(caplog, name):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger(name).info("GET https://api.helius.xyz/v0/a/b?api-key=%s&type=SWAP", KEY)
        logging.getLogger(name).debug("url=%s", f"https://x/?api-key={KEY}")
    assert KEY not in caplog.text
    assert "api-key=***" in caplog.text


def test_unrelated_loggers_are_left_alone(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("otro").info("api-key=visible")
    assert "api-key=visible" in caplog.text
