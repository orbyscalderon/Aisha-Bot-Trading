"""Log de auditoría append-only (spec §7.8).

La inmutabilidad la garantizan los triggers BEFORE UPDATE / BEFORE DELETE que
crea storage/db.py (Tarea 3); esta clase solo inserta y lee.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from storage.repository import _parse_ts, _ts


@dataclass(frozen=True)
class AuditEvent:
    id: int
    timestamp: datetime
    event: str
    payload: dict


class AuditLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        """Cierra la conexión (para quien creó un AuditLog con conexión propia)."""
        self._conn.close()

    def record(self, event: str, payload: dict, now: datetime | None = None) -> int:
        """Añade un evento y devuelve el id de la fila."""
        ts = _ts(now if now is not None else datetime.now(timezone.utc))
        body = json.dumps(payload, default=str)  # Decimal/datetime viajan como texto
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO audit_log (timestamp, event, payload) VALUES (?, ?, ?)",
                (ts, event, body),
            )
        return cur.lastrowid

    def events(
        self, since: datetime | None = None, event: str | None = None
    ) -> list[AuditEvent]:
        """Eventos en orden de inserción; `since` es inclusivo."""
        sql = "SELECT id, timestamp, event, payload FROM audit_log"
        clauses, params = [], []
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(_ts(since))
        if event is not None:
            clauses.append("event = ?")
            params.append(event)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [
            AuditEvent(r["id"], _parse_ts(r["timestamp"]), r["event"], json.loads(r["payload"]))
            for r in self._conn.execute(sql, params)
        ]
