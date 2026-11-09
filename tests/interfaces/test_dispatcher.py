"""Tests del dispatcher: quién puede mandar, qué se ejecuta y qué queda registrado."""

from __future__ import annotations

import sqlite3

import pytest

from core.safety import is_kill_switch_active, is_paused, pause
from interfaces.commands import CommandName
from interfaces.dispatcher import Dispatcher
from interfaces.nlu.rules import RulesParser
from storage.audit import AuditLog
from storage.db import init_db

YO = "123456"
OTRO = "999999"


class FakeReports:
    def __init__(self):
        self.calls = []

    def status(self) -> str:
        self.calls.append("status")
        return "modo: paper | 2 posiciones"

    def positions(self) -> str:
        self.calls.append("positions")
        return "BONK/USDC +3,2%"

    def pnl(self, period: str | None) -> str:
        self.calls.append(f"pnl:{period}")
        return "PnL 7d: +12,40 USD"


@pytest.fixture
def audit(tmp_path):
    return AuditLog(init_db(tmp_path / "bot.sqlite"))


@pytest.fixture
def make(tmp_path, audit):
    def _make(authorized=(YO,)):
        reports = FakeReports()
        d = Dispatcher(
            root=tmp_path, authorized=frozenset(authorized),
            parsers=[RulesParser()], reports=reports, audit=audit,
        )
        return d, reports
    return _make


# --- autorización ---

def test_un_remitente_no_autorizado_no_recibe_NADA(make):
    """Diseño §10.4: se ignora sin responder, para no confirmar siquiera que el bot existe."""
    d, reports = make()
    assert d.handle(OTRO, "/status") is None
    assert reports.calls == []


def test_una_allowlist_vacia_no_autoriza_a_nadie(make):
    """Falla cerrado. Si una allowlist mal configurada significara 'todos', un .env a medio
    rellenar abriría el bot al mundo."""
    d, _ = make(authorized=())
    assert d.handle(YO, "/status") is None


def test_el_remitente_autorizado_si_recibe_respuesta(make):
    d, reports = make()
    assert "paper" in d.handle(YO, "/status")
    assert reports.calls == ["status"]


def test_el_remitente_se_compara_como_texto(make):
    """Telegram manda el chat_id como entero; la allowlist viene del .env como texto."""
    d, _ = make()
    assert d.handle(123456, "/status") is not None


def test_un_intento_no_autorizado_queda_registrado(make, audit):
    """No se le responde, pero sí se anota: saber que alguien lo intenta importa."""
    d, _ = make()
    d.handle(OTRO, "/stop")
    eventos = [e for e in audit.events() if e.event == "chat_unauthorized"]
    assert len(eventos) == 1 and eventos[0].payload["sender"] == OTRO


def test_el_texto_de_un_no_autorizado_no_se_guarda_entero(make, audit):
    """Registrar el contenido de mensajes ajenos es guardar basura de desconocidos."""
    d, _ = make()
    d.handle(OTRO, "secreto muy largo que no deberia guardarse entero")
    evento = [e for e in audit.events() if e.event == "chat_unauthorized"][0]
    assert "secreto" not in str(evento.payload)


# --- consultas ---

def test_positions_y_pnl_llegan_a_los_informes(make):
    d, reports = make()
    assert "BONK" in d.handle(YO, "/positions")
    assert "12,40" in d.handle(YO, "/pnl 7d")
    assert reports.calls == ["positions", "pnl:7d"]


def test_pnl_sin_periodo_pasa_none(make):
    d, reports = make()
    d.handle(YO, "/pnl")
    assert reports.calls == ["pnl:None"]


def test_las_consultas_no_se_auditan(make, audit):
    """Solo se registra lo que cambia el estado; si no, el log se llena de ruido."""
    d, _ = make()
    d.handle(YO, "/status")
    d.handle(YO, "/positions")
    assert [e for e in audit.events() if e.event.startswith("chat_command")] == []


# --- acciones ---

def test_pause_detiene_de_verdad(make, tmp_path):
    d, _ = make()
    respuesta = d.handle(YO, "/pausa")
    assert is_paused(tmp_path) is True
    assert "pausa" in respuesta.lower()


def test_resume_reanuda_de_verdad(make, tmp_path):
    d, _ = make()
    pause(tmp_path, "antes")
    d.handle(YO, "/reanudar")
    assert is_paused(tmp_path) is False


def test_reanudar_sin_pausa_lo_dice_en_vez_de_mentir(make):
    d, _ = make()
    assert "no estaba" in d.handle(YO, "/reanudar").lower()


def test_stop_activa_el_kill_switch(make, tmp_path):
    d, _ = make()
    respuesta = d.handle(YO, "/parar")
    assert is_kill_switch_active(tmp_path) is True
    assert "terminal" in respuesta.lower()  # dice cómo deshacerlo, porque no se puede por chat


def test_stop_dos_veces_no_falla(make, tmp_path):
    d, _ = make()
    d.handle(YO, "/stop")
    assert d.handle(YO, "/stop") is not None
    assert is_kill_switch_active(tmp_path) is True


def test_las_acciones_quedan_auditadas_con_el_remitente(make, audit):
    d, _ = make()
    d.handle(YO, "/pausa")
    d.handle(YO, "/parar")
    eventos = [e for e in audit.events() if e.event == "chat_command"]
    assert [e.payload["command"] for e in eventos] == ["PAUSE", "STOP"]
    assert all(e.payload["sender"] == YO for e in eventos)


# --- lo que no entiende ---

def test_un_mensaje_incomprensible_devuelve_la_ayuda_y_no_hace_nada(make, tmp_path):
    d, _ = make()
    respuesta = d.handle(YO, "vendelo todo ya")
    assert "/status" in respuesta
    assert is_paused(tmp_path) is False and is_kill_switch_active(tmp_path) is False


def test_help_lista_los_comandos(make):
    d, _ = make()
    respuesta = d.handle(YO, "/ayuda")
    for esperado in ("/status", "/positions", "/pnl", "/pause", "/resume", "/stop"):
        assert esperado in respuesta


def test_un_informe_que_revienta_no_tumba_el_bot(make):
    """Si una consulta falla, el usuario debe enterarse por mensaje, no por un bot muerto."""
    d, reports = make()
    reports.status = lambda: 1 / 0
    respuesta = d.handle(YO, "/status")
    assert respuesta is not None and "error" in respuesta.lower()
