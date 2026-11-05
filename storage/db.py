"""Esquema SQLite y apertura de la conexión.

Este módulo es dueño del esquema completo (siete tablas). Ninguna otra parte
del código ejecuta DDL.

Convenios de almacenamiento (la conversión es explícita, ver repository.py):
- Decimal -> TEXT (str(d)); nunca REAL, que lo pasaría a float.
- datetime -> TEXT ISO-8601 UTC con formato fijo, de modo que el orden
  lexicográfico coincide con el cronológico.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id           TEXT PRIMARY KEY,
    signal_id    TEXT NOT NULL,
    trader_id    TEXT NOT NULL,
    market       TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    entry_price  TEXT NOT NULL,
    quantity     TEXT NOT NULL,
    amount_usd   TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    peak_price   TEXT NOT NULL,
    closed_at    TEXT,
    exit_reason  TEXT,
    exit_price   TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions (closed_at);

CREATE TABLE IF NOT EXISTS fills (
    id               TEXT PRIMARY KEY,
    client_order_id  TEXT NOT NULL,
    market           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    price            TEXT NOT NULL,
    quantity         TEXT NOT NULL,
    fee_usd          TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    position_id      TEXT REFERENCES positions (id)
);
CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fills (timestamp);

CREATE TABLE IF NOT EXISTS cursors (
    source  TEXT PRIMARY KEY,
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frozen_allowlist (
    session_id  TEXT PRIMARY KEY,
    traders     TEXT NOT NULL
);

-- La PRIMARY KEY no es decorativa: la idempotencia de órdenes (Tarea 8)
-- detecta duplicados capturando el IntegrityError del INSERT.
CREATE TABLE IF NOT EXISTS order_intents (
    client_order_id  TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    payload          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    error            TEXT
);

-- Estado pequeño del bot que debe sobrevivir a un reinicio (p. ej. el pico de
-- capital de la protección de drawdown). Decimales como texto.
CREATE TABLE IF NOT EXISTS bot_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Solo-añadir por construcción: los triggers impiden UPDATE y DELETE.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    event      TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE not allowed');
END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE not allowed');
END;
"""


def init_db(path: Path) -> sqlite3.Connection:
    """Abre (o crea) la base de datos y asegura el esquema. Idempotente."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    return conn
