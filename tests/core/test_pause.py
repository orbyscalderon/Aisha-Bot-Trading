"""Tests de la pausa: detiene las APERTURAS y deja vivas las salidas.

Es la diferencia con el kill switch. Pausar un bot y que deje de vigilar los stop-loss de lo que
ya tiene abierto sería peor que no pausarlo: te quedas con posiciones y sin quien las cierre.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.safety import (
    KILL_SWITCH_FILENAME,
    PAUSE_FILENAME,
    activate_kill_switch,
    is_kill_switch_active,
    is_paused,
    pause,
    resume,
)


def test_sin_fichero_no_hay_pausa(tmp_path):
    assert is_paused(tmp_path) is False


def test_pausar_crea_el_fichero_y_se_nota(tmp_path):
    pause(tmp_path, "lo pidió orbys por Telegram")
    assert is_paused(tmp_path) is True
    assert (tmp_path / PAUSE_FILENAME).exists()


def test_el_fichero_de_pausa_dice_quien_y_por_que(tmp_path):
    """Al volver a la máquina y encontrarla pausada, el fichero debe explicarse solo."""
    pause(tmp_path, "lo pidió orbys por Telegram")
    assert "orbys" in (tmp_path / PAUSE_FILENAME).read_text(encoding="utf-8")


def test_reanudar_devuelve_si_habia_algo_que_reanudar(tmp_path):
    """El dispatcher usa el valor para responder 'ya estaba en marcha' en vez de mentir."""
    assert resume(tmp_path) is False
    pause(tmp_path, "x")
    assert resume(tmp_path) is True
    assert is_paused(tmp_path) is False


def test_pausar_dos_veces_no_falla(tmp_path):
    pause(tmp_path, "primera")
    pause(tmp_path, "segunda")
    assert is_paused(tmp_path) is True
    assert "segunda" in (tmp_path / PAUSE_FILENAME).read_text(encoding="utf-8")


def test_la_pausa_y_el_kill_switch_son_independientes(tmp_path):
    """Son dos cosas distintas: una se deshace desde el chat, la otra no."""
    pause(tmp_path, "x")
    assert is_kill_switch_active(tmp_path) is False
    resume(tmp_path)
    activate_kill_switch(tmp_path, "pánico")
    assert is_paused(tmp_path) is False
    assert is_kill_switch_active(tmp_path) is True


def test_el_kill_switch_se_activa_con_motivo(tmp_path):
    activate_kill_switch(tmp_path, "lo pidió orbys por Telegram")
    assert (tmp_path / KILL_SWITCH_FILENAME).exists()
    assert "orbys" in (tmp_path / KILL_SWITCH_FILENAME).read_text(encoding="utf-8")


def test_no_hay_forma_de_desactivar_el_kill_switch_por_codigo():
    """Asimetría deliberada (diseño §10.3): parar debe ser más fácil que arrancar. La pausa se
    deshace desde el chat; el kill switch exige ir a la máquina y borrar el fichero, porque un
    mensaje se puede enviar desde un móvil desbloqueado o desde una frase mal interpretada."""
    import core.safety as safety

    assert not hasattr(safety, "deactivate_kill_switch")
    assert not hasattr(safety, "clear_kill_switch")


def test_una_raiz_que_no_existe_no_revienta_al_consultar(tmp_path):
    assert is_paused(tmp_path / "no-existe") is False
    assert is_kill_switch_active(tmp_path / "no-existe") is False
