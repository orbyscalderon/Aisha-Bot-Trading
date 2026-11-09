"""Tests del cargador de `.env`.

El fallo que originó este módulo: solo HELIUS_API_KEY se leía del fichero, así que la tarea
programada arrancaba el bot sin Telegram y sin decir nada.
"""

from __future__ import annotations

import pytest

from adapters.env import load_dotenv, parse_dotenv, read_dotenv


def escribe(tmp_path, texto, encoding="utf-8"):
    (tmp_path / ".env").write_text(texto, encoding=encoding)
    return tmp_path


# --- lo que trae un fichero real ---

def test_lee_un_par_normal():
    assert parse_dotenv("CLAVE=valor") == {"CLAVE": "valor"}


def test_ignora_comentarios_y_lineas_en_blanco():
    assert parse_dotenv("# nota\n\nCLAVE=v\n\n# otra\n") == {"CLAVE": "v"}


def test_aguanta_finales_de_linea_de_windows():
    """El .env del proyecto es CRLF: sin esto el valor se lleva un \\r pegado y el token falla."""
    assert parse_dotenv("CLAVE=valor\r\nOTRA=2\r\n") == {"CLAVE": "valor", "OTRA": "2"}


def test_aguanta_el_bom(tmp_path):
    assert read_dotenv(escribe(tmp_path, "CLAVE=v", encoding="utf-8-sig")) == {"CLAVE": "v"}


def test_acepta_export_delante():
    assert parse_dotenv("export CLAVE=v") == {"CLAVE": "v"}


def test_quita_las_comillas():
    assert parse_dotenv("A='v'\nB=\"w\"") == {"A": "v", "B": "w"}


def test_un_valor_con_signos_igual_se_respeta_entero():
    """Las claves en base64 terminan en '='. Partir por el primer '=' y no por el ultimo."""
    assert parse_dotenv("CLAVE=abc=def==") == {"CLAVE": "abc=def=="}


def test_una_clave_vacia_se_lee_como_vacia():
    assert parse_dotenv("CLAVE=") == {"CLAVE": ""}


def test_una_linea_sin_igual_se_ignora_sin_romper():
    """Un .env a medio escribir no debe impedir arrancar con las claves que si estan bien."""
    assert parse_dotenv("esto no es nada\nCLAVE=v") == {"CLAVE": "v"}


def test_un_comentario_al_final_del_valor_NO_se_recorta():
    """Recortar en silencio parte de un secreto da un 'token invalido' indiagnosticable."""
    assert parse_dotenv("CLAVE=abc # nota") == {"CLAVE": "abc # nota"}


# --- el fichero que no está ---

def test_sin_fichero_no_revienta(tmp_path):
    assert read_dotenv(tmp_path) == {}


def test_una_carpeta_inexistente_no_revienta(tmp_path):
    assert read_dotenv(tmp_path / "no" / "existe") == {}


# --- volcarlo en el entorno ---

def test_carga_lo_que_falta(tmp_path):
    entorno: dict[str, str] = {}
    cargados = load_dotenv(escribe(tmp_path, "TELEGRAM_BOT_TOKEN=123:abc"), entorno)
    assert entorno["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert cargados == ["TELEGRAM_BOT_TOKEN"]


def test_el_entorno_real_gana_al_fichero(tmp_path):
    """Un .env olvidado en el disco no puede silenciar lo que un servicio o un CI configuro
    de forma explicita: ahi es donde mas caro sale equivocarse."""
    entorno = {"CLAVE": "del-entorno"}
    cargados = load_dotenv(escribe(tmp_path, "CLAVE=del-fichero"), entorno)
    assert entorno["CLAVE"] == "del-entorno"
    assert cargados == []


def test_una_variable_vacia_en_el_entorno_si_se_rellena(tmp_path):
    """Exportar CLAVE= sin valor es no tenerla, no tenerla en blanco a proposito."""
    entorno = {"CLAVE": ""}
    load_dotenv(escribe(tmp_path, "CLAVE=v"), entorno)
    assert entorno["CLAVE"] == "v"


def test_una_clave_vacia_en_el_fichero_no_se_carga(tmp_path):
    """Cargarla como cadena vacia haria que un 'if not token' siguiera siendo falso."""
    entorno: dict[str, str] = {}
    assert load_dotenv(escribe(tmp_path, "CLAVE="), entorno) == []
    assert "CLAVE" not in entorno


def test_only_limita_lo_que_se_carga(tmp_path):
    entorno: dict[str, str] = {}
    load_dotenv(escribe(tmp_path, "A=1\nB=2"), entorno, only={"A"})
    assert entorno == {"A": "1"}


def test_devuelve_nombres_nunca_valores(tmp_path):
    """Lo que se devuelve acaba en un log. Los secretos no."""
    cargados = load_dotenv(escribe(tmp_path, "TOKEN=secreto-que-no-debe-salir"), {})
    assert cargados == ["TOKEN"]
    assert "secreto-que-no-debe-salir" not in str(cargados)


def test_no_toca_el_fichero(tmp_path):
    raiz = escribe(tmp_path, "CLAVE=v\n")
    load_dotenv(raiz, {})
    assert (raiz / ".env").read_text(encoding="utf-8") == "CLAVE=v\n"
