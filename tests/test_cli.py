"""Tests de la CLI. El planificador, la fuente de datos y el notificador se inyectan:
ningún test espera tiempo real ni toca la red."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import yaml
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from typer.testing import CliRunner

import cli
from adapters.notify.console import ConsoleNotifier
from adapters.notify.telegram import TelegramNotifier
from cli import CliDeps, app, build_engine
from core.config import load_config
from core.models import Quote
from ports.market import MarketDataSource
from ports.notifier import Level, Notifier

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

BASE = {
    "mode": "paper",
    "sizing": {"strategy": "flat", "flat_amount_usd": 100},
    "exits": {"stop_loss_pct": 15, "primary": "copy_sell"},
    "risk": {
        "max_usd_per_token": 1000, "max_open_positions": 5, "max_usd_per_day": 5000,
        "max_daily_loss_usd": 500, "hard_max_order_usd": 1000,
    },
    "cadence": {"entry_cron": "0 * * * *", "exit_interval_seconds": 60},
    "protections": {"cooldown_minutes": 60, "stop_loss_streak": 3, "max_drawdown_pct": 10},
}


def write_yaml(path, **overrides):
    data = {**BASE, **overrides}
    for k in [k for k, v in data.items() if v is None]:
        del data[k]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def paper_yaml(tmp_path):
    return write_yaml(tmp_path / "paper.yaml")


@pytest.fixture
def live_yaml(tmp_path):
    return write_yaml(tmp_path / "live.yaml", mode="live")


@pytest.fixture
def signals_csv(tmp_path):
    p = tmp_path / "signals.csv"
    p.write_text(
        "timestamp,trader_id,symbol,side,amount_usd\n"
        f"{T0.isoformat()},t1,BTC/USDT,buy,100\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def quotes_csv(tmp_path):
    p = tmp_path / "quotes.csv"
    rows = ["timestamp,symbol,price"]
    for i, price in enumerate([100, 100, 70, 70]):
        rows.append(f"{(T0 + timedelta(hours=i)).isoformat()},BTC/USDT,{price}")
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


class FakeData(MarketDataSource):
    def get_quote(self, symbol):
        return Quote(symbol, Decimal(100), datetime.now(UTC))

    def get_balance(self):
        return Decimal(10000)


class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, Level]] = []

    def send(self, message, level):
        self.sent.append((message, level))


class FakeScheduler:
    def __init__(self):
        self.jobs: list[dict] = []
        self.started = False
        self.stopped = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.stopped = True


def invoke(args, deps=None):
    return CliRunner().invoke(app, args, obj=deps or CliDeps(data_source=FakeData()))


# --- run: modo ---

def test_run_refuses_live_mode_in_this_plan(live_yaml, signals_csv, root):
    r = invoke(["run", "--config", str(live_yaml), "--signals", str(signals_csv),
                "--root", str(root), "--once"])
    assert r.exit_code != 0
    assert "no implementado" in r.output
    assert "plan 3" in r.output
    # no se ejecutó nada, ni siquiera en simulado
    assert not list(root.glob("*.db"))


def test_run_live_never_starts_scheduler(live_yaml, signals_csv, root):
    sched = FakeScheduler()
    invoke(["run", "--config", str(live_yaml), "--signals", str(signals_csv),
            "--root", str(root)], CliDeps(scheduler=sched, data_source=FakeData()))
    assert sched.jobs == [] and not sched.started


def test_build_engine_refuses_live(live_yaml, root):
    with pytest.raises(cli.LiveModeNotImplemented):
        build_engine(load_config(live_yaml), root)


def test_run_announces_paper_mode(paper_yaml, signals_csv, root):
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--root", str(root), "--once"])
    assert r.exit_code == 0, r.output
    assert "mode: paper" in r.output


def test_run_once_runs_one_entry_and_one_exit_cycle(paper_yaml, signals_csv, root):
    sched = FakeScheduler()
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--root", str(root), "--once"], CliDeps(scheduler=sched, data_source=FakeData()))
    assert r.exit_code == 0, r.output
    assert "ciclo de entrada" in r.output and "ciclo de salida" in r.output
    # --once no usa planificador
    assert sched.jobs == [] and not sched.started


def test_run_requires_signals_file(paper_yaml, root):
    r = invoke(["run", "--config", str(paper_yaml), "--root", str(root), "--once"])
    assert r.exit_code != 0
    assert "--signals" in r.output and "Traceback" not in r.output


# --- planificador ---

def test_run_loop_uses_configured_cadence(paper_yaml, signals_csv, root):
    # spec §4.2: dos bucles independientes, entrada por cron y salida por intervalo
    sched = FakeScheduler()
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--root", str(root)], CliDeps(scheduler=sched, data_source=FakeData()))
    assert r.exit_code == 0, r.output
    assert sched.started and len(sched.jobs) == 2
    entry, exit_ = sched.jobs
    assert isinstance(entry["trigger"], CronTrigger)
    assert str(entry["trigger"]) == str(CronTrigger.from_crontab("0 * * * *", timezone=UTC))
    assert isinstance(exit_["trigger"], IntervalTrigger)
    assert exit_["trigger"].interval == timedelta(seconds=60)


def test_jobs_never_overlap_and_coalesce(paper_yaml, signals_csv, root):
    sched = FakeScheduler()
    invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
            "--root", str(root)], CliDeps(scheduler=sched, data_source=FakeData()))
    for job in sched.jobs:
        assert job["max_instances"] == 1
        assert job["coalesce"] is True
        # sin esto, un ciclo atrasado más de 1 s (equipo suspendido) se descartaría
        assert job["misfire_grace_time"] is None


def test_cron_is_in_utc(paper_yaml, signals_csv, root):
    sched = FakeScheduler()
    invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
            "--root", str(root)], CliDeps(scheduler=sched, data_source=FakeData()))
    assert str(sched.jobs[0]["trigger"].timezone) == "UTC"


def test_invalid_cron_is_a_message_not_a_traceback(tmp_path, signals_csv, root):
    bad = write_yaml(
        tmp_path / "badcron.yaml",
        cadence={"entry_cron": "no es un cron", "exit_interval_seconds": 60},
    )
    r = invoke(["run", "--config", str(bad), "--signals", str(signals_csv), "--root", str(root)],
               CliDeps(scheduler=FakeScheduler(), data_source=FakeData()))
    assert r.exit_code != 0 and "entry_cron" in r.output and "Traceback" not in r.output


def test_exit_loop_runs_more_often_than_entry_loop(paper_yaml):
    # un stop-loss comprobado una vez por hora no es un stop-loss
    cfg = load_config(paper_yaml).cadence
    assert cfg.exit_interval_seconds < 3600


def test_default_scheduler_is_a_blocking_apscheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    assert isinstance(cli.default_scheduler(), BlockingScheduler)


def test_entry_and_exit_cycles_are_serialised():
    active = {"n": 0, "max": 0}
    guard = threading.Lock()

    class SlowEngine:
        def _cycle(self):
            with guard:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.05)
            with guard:
                active["n"] -= 1

        def run_entry_cycle(self, source, now):
            self._cycle()

        def run_exit_cycle(self, now):
            self._cycle()

    entry, exit_ = cli.build_jobs(SlowEngine(), object())
    threads = [threading.Thread(target=f) for f in (entry, exit_, entry, exit_)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1


def test_engine_database_is_usable_from_scheduler_threads(paper_yaml, root):
    engine = build_engine(load_config(paper_yaml), root, data_source=FakeData(),
                          notifier=RecordingNotifier())
    errors = []

    def work():
        try:
            engine.run_exit_cycle(datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert errors == []


# --- protecciones omitidas ---

def test_live_requires_protections_key(tmp_path, signals_csv, root):
    p = write_yaml(tmp_path / "live_np.yaml", mode="live", protections=None)
    r = invoke(["run", "--config", str(p), "--signals", str(signals_csv), "--root", str(root)])
    assert r.exit_code != 0
    assert "protections" in r.output and "Traceback" not in r.output
    assert "no implementado" not in r.output  # falla antes: es un error de configuración


def test_live_accepts_explicit_zero_protections(tmp_path):
    p = write_yaml(tmp_path / "live_zero.yaml", mode="live", protections={"cooldown_minutes": 0})
    assert load_config(p).mode.value == "live"


def test_paper_warns_when_protections_omitted(tmp_path, signals_csv, root):
    p = write_yaml(tmp_path / "np.yaml", protections=None)
    notifier = RecordingNotifier()
    r = invoke(["run", "--config", str(p), "--signals", str(signals_csv), "--root", str(root), "--once"],
               CliDeps(data_source=FakeData(), notifier=notifier))
    assert r.exit_code == 0, r.output
    for name in ("cooldown_minutes", "stop_loss_streak", "max_drawdown_pct", "max_usd_per_group"):
        assert name in r.output
    warnings = [m for m, lvl in notifier.sent if lvl is Level.WARNING and "cooldown_minutes" in m]
    assert warnings


def test_paper_warns_when_protections_all_zero(tmp_path, signals_csv, root):
    p = write_yaml(tmp_path / "zero.yaml", protections={"cooldown_minutes": 0})
    r = invoke(["run", "--config", str(p), "--signals", str(signals_csv), "--root", str(root), "--once"])
    assert "sin protecciones" in r.output


def test_no_warning_when_some_protection_is_on(paper_yaml, signals_csv, root):
    notifier = RecordingNotifier()
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--root", str(root), "--once"], CliDeps(data_source=FakeData(), notifier=notifier))
    assert "sin protecciones" not in r.output
    assert not [m for m, lvl in notifier.sent if lvl is Level.WARNING and "protecciones" in m]


def test_backtest_warns_when_protections_omitted(tmp_path, signals_csv, quotes_csv):
    p = write_yaml(tmp_path / "np.yaml", protections=None)
    r = invoke(["backtest", "--config", str(p), "--signals", str(signals_csv), "--quotes", str(quotes_csv)])
    assert r.exit_code == 0, r.output
    assert "sin protecciones" in r.output


# --- I1: copy_sell no está conectado en el plan 1 ---

def test_run_warns_that_copy_sell_is_not_wired(paper_yaml, signals_csv, root):
    notifier = RecordingNotifier()
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--root", str(root), "--once"], CliDeps(data_source=FakeData(), notifier=notifier))
    assert r.exit_code == 0, r.output
    assert "copy_sell" in r.output and "stop-loss" in r.output and "time-stop" in r.output
    assert [m for m, lvl in notifier.sent if lvl is Level.WARNING and "copy_sell" in m]


def test_backtest_warns_that_copy_sell_is_not_wired(paper_yaml, signals_csv, quotes_csv):
    r = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--quotes", str(quotes_csv)])
    assert r.exit_code == 0, r.output
    assert "copy_sell" in r.output


def test_no_copy_sell_warning_for_other_primaries(tmp_path, signals_csv, root):
    p = write_yaml(tmp_path / "tp.yaml", exits={"stop_loss_pct": 15, "primary": "time_stop"})
    r = invoke(["run", "--config", str(p), "--signals", str(signals_csv),
                "--root", str(root), "--once"])
    assert "copy_sell" not in r.output


# --- stop / status / positions ---

def test_stop_creates_kill_switch_file(root):
    r = invoke(["stop", "--root", str(root)])
    assert r.exit_code == 0
    assert (root / "STOP").exists()


def test_status_reports_mode_and_open_positions(paper_yaml, root):
    out = invoke(["status", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "mode: paper" in out
    assert "posiciones abiertas: 0" in out
    assert "kill switch: inactivo" in out


def test_status_shows_kill_switch_active(paper_yaml, root):
    invoke(["stop", "--root", str(root)])
    out = invoke(["status", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "kill switch: ACTIVO" in out


def test_status_lists_unconfirmed_intents(paper_yaml, root):
    # Una orden en SENT (resultado incierto) es lo que bloquea una salida: el operador
    # debe verla aquí, no enterarse por un log.
    from datetime import datetime, timezone
    from storage.db import init_db
    from storage.repository import Repository
    cfg = load_config(paper_yaml)
    conn = init_db(root / f"{cfg.mode.value}.db")
    repo = Repository(conn)
    repo.reserve_intent("exit-abc", {"position_id": "p1", "reason": "stop_loss"},
                        datetime(2026, 1, 1, tzinfo=timezone.utc))
    repo.mark_intent_sent("exit-abc")
    conn.close()
    out = invoke(["status", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "órdenes sin confirmar: 1" in out
    assert "exit-abc" in out and "sent" in out.lower() and "p1" in out


def test_status_reports_zero_unconfirmed_intents(paper_yaml, root):
    out = invoke(["status", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "órdenes sin confirmar: 0" in out


def test_build_engine_closes_the_side_audit_connection_it_drops(paper_yaml, root, monkeypatch):
    closed = []
    real = cli._build

    def spy(*a, **kw):
        engine, notifier, audit = real(*a, **kw)
        orig = audit.close
        audit.close = lambda: (closed.append(True), orig())[1]
        return engine, notifier, audit

    monkeypatch.setattr(cli, "_build", spy)
    build_engine(load_config(paper_yaml), root, data_source=FakeData())
    assert closed == [True]


def test_positions_lists_open_positions(paper_yaml, root):
    from core.models import Market, Position
    from storage.db import init_db
    from storage.repository import Repository

    conn = init_db(root / "paper.db")
    Repository(conn).save_position(Position(
        "p1", "s1", "t1", Market.CEX, "ETH/USDT", Decimal(2000), Decimal("0.05"),
        Decimal(100), T0, Decimal(2000)))
    conn.close()
    out = invoke(["positions", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "ETH/USDT" in out and "mode: paper" in out
    status = invoke(["status", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "posiciones abiertas: 1" in status


def test_positions_when_none(paper_yaml, root):
    out = invoke(["positions", "--config", str(paper_yaml), "--root", str(root)]).output
    assert "sin posiciones abiertas" in out


# --- backtest ---

def test_backtest_prints_closes_by_reason(paper_yaml, signals_csv, quotes_csv):
    r = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--quotes", str(quotes_csv)])
    assert r.exit_code == 0, r.output
    assert "STOP_LOSS" in r.output
    assert "mode: paper" in r.output


def test_backtest_prints_zero_open_positions_explicitly(paper_yaml, signals_csv, quotes_csv):
    out = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv),
                  "--quotes", str(quotes_csv)]).output
    assert "Posiciones abiertas al final: 0" in out
    assert "PnL latente: 0.00 USD" in out


def test_backtest_prints_open_positions_and_unrealized_pnl(paper_yaml, signals_csv, tmp_path):
    # cae un 5 %: no salta el stop-loss del 15 %, la posición sigue abierta y perdiendo
    q = tmp_path / "falling.csv"
    q.write_text(
        "timestamp,symbol,price\n"
        f"{T0.isoformat()},BTC/USDT,100\n"
        f"{(T0 + timedelta(hours=2)).isoformat()},BTC/USDT,95\n",
        encoding="utf-8",
    )
    out = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv),
                  "--quotes", str(q)]).output
    assert "Posiciones abiertas al final: 1" in out
    assert "PnL latente: -" in out


def test_backtest_writes_html_report(paper_yaml, signals_csv, quotes_csv, tmp_path):
    report = tmp_path / "out" / "report.html"
    r = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--quotes", str(quotes_csv), "--report", str(report)])
    assert r.exit_code == 0, r.output
    assert report.exists() and report.stat().st_size > 0
    assert "report.html" in r.output


def test_backtest_bad_csv_is_a_message(paper_yaml, signals_csv, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp,symbol,price\nayer,BTC/USDT,xx\n", encoding="utf-8")
    r = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv), "--quotes", str(bad)])
    assert r.exit_code != 0 and "Traceback" not in r.output and "bad.csv" in r.output


def test_backtest_missing_file_is_a_message(paper_yaml, signals_csv, tmp_path):
    r = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(signals_csv),
                "--quotes", str(tmp_path / "nope.csv")])
    assert r.exit_code != 0 and "Traceback" not in r.output


# --- errores de configuración ---

@pytest.mark.parametrize("command", ["status", "positions", "run", "backtest"])
def test_invalid_config_exits_with_message_not_traceback(tmp_path, command):
    extra = ["--signals", "s.csv", "--quotes", "q.csv"] if command == "backtest" else []
    broken = tmp_path / "broken.yaml"
    broken.write_text("mode: paper\nmodee: x\n", encoding="utf-8")
    r = invoke([command, "--config", str(broken), *extra])
    assert r.exit_code != 0
    assert "Traceback" not in r.output
    assert "modee" in r.output


def test_missing_config_exits_with_message(tmp_path):
    r = invoke(["status", "--config", str(tmp_path / "nope.yaml")])
    assert r.exit_code != 0 and "Traceback" not in r.output


# --- build_engine ---

def test_build_engine_uses_console_notifier_by_default(paper_yaml, root, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    engine = build_engine(load_config(paper_yaml), root, data_source=FakeData())
    assert isinstance(engine._notifier, ConsoleNotifier)


def test_build_engine_uses_telegram_when_configured(paper_yaml, root, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    engine = build_engine(load_config(paper_yaml), root, data_source=FakeData())
    assert isinstance(engine._notifier, TelegramNotifier)


def test_paper_balance_is_simulated_not_the_real_account(paper_yaml, root):
    class RealAccount(FakeData):
        def get_balance(self):
            raise AssertionError("el modo simulado no debe consultar la cuenta real")

    engine = build_engine(load_config(paper_yaml), root, data_source=RealAccount(),
                          paper_capital=Decimal(5000))
    assert engine._data.get_balance() == Decimal(5000)


def test_build_engine_passes_ignore_assets_to_the_cex_adapter(tmp_path, root, monkeypatch):
    seen = {}

    class SpyCex(FakeData):
        def __init__(self, exchange_id, *args, **kwargs):
            seen["exchange_id"] = exchange_id
            seen["ignore_assets"] = kwargs.get("ignore_assets")

    monkeypatch.setattr(cli, "CexDataSource", SpyCex)
    p = write_yaml(tmp_path / "cex.yaml", cex={"ignore_assets": ["LDBNB", "FAUCET"]})
    build_engine(load_config(p), root, notifier=RecordingNotifier())
    assert list(seen["ignore_assets"]) == ["LDBNB", "FAUCET"]


# --- cerrojo con tiempo límite: las salidas nunca se quedan esperando en silencio ---

class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event, payload, now=None):
        self.events.append((event, payload))


def test_exit_cycle_times_out_notifies_critical_and_does_not_hang():
    release = threading.Event()
    started = threading.Event()
    calls = []

    class HungEntryEngine:
        def run_entry_cycle(self, source, now):
            started.set()
            release.wait(5)

        def run_exit_cycle(self, now):
            calls.append("exit")

    notifier, audit = RecordingNotifier(), FakeAudit()
    entry, exit_ = cli.build_jobs(HungEntryEngine(), object(), notifier=notifier,
                                  audit=audit, lock_timeout=0.1)
    t = threading.Thread(target=entry)
    t.start()
    assert started.wait(2)
    began = time.monotonic()
    exit_()  # no debe colgarse
    elapsed = time.monotonic() - began
    release.set()
    t.join()
    assert elapsed < 2
    assert calls == []  # sin cerrojo no se fuerza la ejecución
    critical = [m for m, lvl in notifier.sent if lvl is Level.CRITICAL]
    assert critical and "salida" in critical[0] and "sin vigilar" in critical[0]
    assert [e for e, _ in audit.events] == ["exit_cycle_skipped_lock_timeout"]


def test_exit_cycle_runs_normally_when_lock_is_free():
    calls = []

    class Eng:
        def run_exit_cycle(self, now):
            calls.append("exit")

    notifier = RecordingNotifier()
    _, exit_ = cli.build_jobs(Eng(), object(), notifier=notifier, audit=FakeAudit(), lock_timeout=0.1)
    exit_()
    assert calls == ["exit"] and notifier.sent == []


def test_lock_timeout_is_longer_than_exit_interval():
    assert cli.exit_lock_timeout(60) > 60
    assert cli.exit_lock_timeout(600) > 600


# --- run no muestra trazas ---

def test_run_validates_signals_csv_at_startup(paper_yaml, tmp_path, root):
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(tmp_path / "nope.csv"),
                "--root", str(root), "--once"])
    assert r.exit_code != 0 and "Traceback" not in r.output and "nope.csv" in r.output


def test_run_malformed_signals_csv_is_a_message(paper_yaml, tmp_path, root):
    bad = tmp_path / "bad_signals.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(bad), "--root", str(root), "--once"])
    assert r.exit_code != 0 and "Traceback" not in r.output and "bad_signals.csv" in r.output


def test_run_unknown_exchange_is_a_message(paper_yaml, signals_csv, root):
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv), "--root", str(root),
                "--once", "--exchange", "noexiste"], CliDeps())
    assert r.exit_code != 0 and "Traceback" not in r.output and "noexiste" in r.output


# --- ronda de correcciones ---

def test_critical_alert_is_sent_even_if_audit_write_fails():
    release, started = threading.Event(), threading.Event()

    class HungEntry:
        def run_entry_cycle(self, source, now):
            started.set()
            release.wait(5)

        def run_exit_cycle(self, now):
            raise AssertionError("no debe ejecutarse sin cerrojo")

    class BrokenAudit:
        def record(self, event, payload, now=None):
            raise RuntimeError("database is locked")

    notifier = RecordingNotifier()
    entry, exit_ = cli.build_jobs(HungEntry(), object(), notifier=notifier,
                                  audit=BrokenAudit(), lock_timeout=0.1)
    t = threading.Thread(target=entry)
    t.start()
    assert started.wait(2)
    try:
        exit_()  # la auditoría rota no puede propagarse
    finally:
        release.set()
        t.join()
    assert [lvl for _, lvl in notifier.sent] == [Level.CRITICAL]


def test_critical_alert_is_sent_before_the_audit_write():
    order = []
    release, started = threading.Event(), threading.Event()

    class HungEntry:
        def run_entry_cycle(self, source, now):
            started.set()
            release.wait(5)

    class Audit:
        def record(self, event, payload, now=None):
            order.append("audit")

    class Notif(Notifier):
        def send(self, message, level):
            order.append("notify")

    entry, exit_ = cli.build_jobs(HungEntry(), object(), notifier=Notif(), audit=Audit(), lock_timeout=0.1)
    t = threading.Thread(target=entry)
    t.start()
    assert started.wait(2)
    try:
        exit_()
    finally:
        release.set()
        t.join()
    assert order == ["notify", "audit"]


def test_lock_is_released_if_exit_cycle_raises():
    class Eng:
        def __init__(self):
            self.n = 0

        def run_exit_cycle(self, now):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")

    eng = Eng()
    notifier = RecordingNotifier()
    _, exit_ = cli.build_jobs(eng, object(), notifier=notifier, lock_timeout=0.1)
    with pytest.raises(RuntimeError):
        exit_()
    exit_()  # si el cerrojo siguiera retenido, esto avisaría y no ejecutaría
    assert eng.n == 2 and notifier.sent == []


def test_lock_timeout_is_bounded_relative_to_interval():
    for interval in (60, 600):
        t = cli.exit_lock_timeout(interval)
        assert interval < t <= 5 * interval


def test_run_wires_audit_and_configured_lock_timeout(tmp_path, signals_csv, root, monkeypatch):
    seen = {}
    real = cli.build_jobs

    def spy(engine, source, **kwargs):
        seen.update(kwargs)
        return real(engine, source, **kwargs)

    monkeypatch.setattr(cli, "build_jobs", spy)
    p = write_yaml(tmp_path / "c120.yaml", cadence={"entry_cron": "0 * * * *", "exit_interval_seconds": 120})
    invoke(["run", "--config", str(p), "--signals", str(signals_csv), "--root", str(root)],
           CliDeps(scheduler=FakeScheduler(), data_source=FakeData()))
    assert seen["audit"] is not None
    assert hasattr(seen["audit"], "record")
    assert seen["lock_timeout"] == cli.exit_lock_timeout(120)


def test_unknown_exchange_creates_no_database(paper_yaml, signals_csv, root):
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv), "--root", str(root),
                "--once", "--exchange", "noexiste"], CliDeps())
    assert r.exit_code != 0
    assert not list(root.glob("*.db"))


def test_unrelated_value_error_is_not_disguised_as_bad_exchange(paper_yaml, signals_csv, root, monkeypatch):
    def boom(*a, **k):
        raise ValueError("fallo interno")

    monkeypatch.setattr(cli, "init_db", boom)
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(signals_csv), "--root", str(root),
                "--once"])
    assert "exchange no válido" not in r.output
    assert isinstance(r.exception, ValueError)


def test_run_truncated_signal_row_is_a_message(paper_yaml, tmp_path, root):
    bad = tmp_path / "trunc.csv"
    bad.write_text(
        "timestamp,trader_id,symbol,side,amount_usd\n"
        f"{T0.isoformat()},t1,BTC/USDT\n",
        encoding="utf-8",
    )
    r = invoke(["run", "--config", str(paper_yaml), "--signals", str(bad), "--root", str(root), "--once"])
    assert r.exit_code != 0 and "Traceback" not in r.output and "trunc.csv" in r.output
    assert not isinstance(r.exception, AttributeError)


def test_backtest_truncated_signal_row_is_a_message(paper_yaml, quotes_csv, tmp_path):
    bad = tmp_path / "trunc.csv"
    bad.write_text(
        "timestamp,trader_id,symbol,side,amount_usd\n"
        f"{T0.isoformat()},t1,BTC/USDT\n",
        encoding="utf-8",
    )
    r = invoke(["backtest", "--config", str(paper_yaml), "--signals", str(bad), "--quotes", str(quotes_csv)])
    assert r.exit_code != 0 and not isinstance(r.exception, AttributeError)


# --- los resultados de los ciclos programados llegan al operador ---

def _exit_job(result):
    class Eng:
        def run_exit_cycle(self, now):
            return result

    notifier = RecordingNotifier()
    _, exit_ = cli.build_jobs(Eng(), object(), notifier=notifier, lock_timeout=0.1)
    exit_()
    return notifier


def test_scheduled_exit_cycle_with_errors_notifies_once_at_warning_with_the_count():
    from core.engine import ExitCycleResult
    n = _exit_job(ExitCycleResult([], ["p1: sin cotización válida para A/USDT",
                                       "p2: sin cotización válida para B/USDT"]))
    assert len(n.sent) == 1
    msg, level = n.sent[0]
    assert level is Level.WARNING and "2" in msg and "A/USDT" in msg


def test_scheduled_clean_exit_cycle_notifies_nothing():
    from core.engine import ExitCycleResult
    assert _exit_job(ExitCycleResult()).sent == []


def test_summary_does_not_duplicate_errors_the_engine_already_alerted():
    from core.engine import ExitCycleResult
    stuck = "p1 (A/USDT): orden atascada, requiere atención manual"
    assert _exit_job(ExitCycleResult([], [stuck], [stuck])).sent == []
    n = _exit_job(ExitCycleResult([], [stuck, "p2: sin cotización"], [stuck]))
    assert len(n.sent) == 1 and "1 error" in n.sent[0][0] and "p1" not in n.sent[0][0]


def test_scheduled_entry_cycle_rejections_do_not_notify(caplog):
    from core.engine import EntryCycleResult

    class Eng:
        def run_entry_cycle(self, source, now):
            return EntryCycleResult(rejected=[("s1", "algo")])

    notifier = RecordingNotifier()
    entry, _ = cli.build_jobs(Eng(), object(), notifier=notifier)
    with caplog.at_level("DEBUG", logger="cli"):
        entry()
    assert notifier.sent == [] and "rechazada" in caplog.text
