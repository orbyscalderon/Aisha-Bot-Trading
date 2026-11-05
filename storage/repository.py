"""Repositorio de persistencia sobre sqlite3 (biblioteca estándar).

Conversión explícita al escribir y leer (sin register_adapter/converter, que
son globales al proceso y exigen detect_types en cada conexión):
- Decimal <-> TEXT con str()/Decimal(str).
- datetime <-> TEXT ISO-8601 UTC de formato fijo; al leer, siempre aware UTC.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from core.idempotency import IntentStatus, OrderIntent
from core.models import ExitReason, Fill, Market, Position, Side

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _dec(value: Decimal) -> str:
    return format(value, "f")  # sin notación científica; ida y vuelta exacta


def _row_to_position(r: sqlite3.Row) -> Position:
    return Position(
        id=r["id"],
        signal_id=r["signal_id"],
        trader_id=r["trader_id"],
        market=Market(r["market"]),
        symbol=r["symbol"],
        entry_price=Decimal(r["entry_price"]),
        quantity=Decimal(r["quantity"]),
        amount_usd=Decimal(r["amount_usd"]),
        opened_at=_parse_ts(r["opened_at"]),
        peak_price=Decimal(r["peak_price"]),
        closed_at=_parse_ts(r["closed_at"]) if r["closed_at"] is not None else None,
        exit_reason=ExitReason(r["exit_reason"]) if r["exit_reason"] is not None else None,
        exit_price=Decimal(r["exit_price"]) if r["exit_price"] is not None else None,
    )


def _row_to_fill(r: sqlite3.Row) -> Fill:
    return Fill(
        id=r["id"],
        client_order_id=r["client_order_id"],
        market=Market(r["market"]),
        symbol=r["symbol"],
        side=Side(r["side"]),
        price=Decimal(r["price"]),
        quantity=Decimal(r["quantity"]),
        fee_usd=Decimal(r["fee_usd"]),
        timestamp=_parse_ts(r["timestamp"]),
        position_id=r["position_id"],
    )


class Repository:
    """Cada método de escritura es una transacción propia (commit al salir)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    # --- posiciones -----------------------------------------------------

    def save_position(self, p: Position) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO positions (id, signal_id, trader_id, market, symbol,"
                " entry_price, quantity, amount_usd, opened_at, peak_price,"
                " closed_at, exit_reason, exit_price)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p.id, p.signal_id, p.trader_id, p.market.value, p.symbol,
                    _dec(p.entry_price), _dec(p.quantity), _dec(p.amount_usd),
                    _ts(p.opened_at), _dec(p.peak_price),
                    _ts(p.closed_at) if p.closed_at is not None else None,
                    p.exit_reason.value if p.exit_reason is not None else None,
                    _dec(p.exit_price) if p.exit_price is not None else None,
                ),
            )

    def get_position(self, id: str) -> Position | None:
        row = self._conn.execute("SELECT * FROM positions WHERE id = ?", (id,)).fetchone()
        return _row_to_position(row) if row is not None else None

    def open_positions(self) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at, rowid"
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def closed_positions_since(self, since: datetime) -> list[Position]:
        """Posiciones cerradas con closed_at >= since, en orden de cierre."""
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE closed_at IS NOT NULL AND closed_at >= ?"
            " ORDER BY closed_at, rowid",
            (_ts(since),),
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def close_position(
        self, id: str, exit_price: Decimal, exit_reason: ExitReason, closed_at: datetime
    ) -> None:
        """Cierra una posición abierta. ValueError si no existe o ya estaba cerrada."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE positions SET exit_price = ?, exit_reason = ?, closed_at = ?"
                " WHERE id = ? AND closed_at IS NULL",
                (_dec(exit_price), ExitReason(exit_reason).value, _ts(closed_at), id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"position {id!r} not found or already closed")

    def update_peak_price(self, id: str, price: Decimal) -> None:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE positions SET peak_price = ? WHERE id = ?", (_dec(price), id)
            )
            if cur.rowcount != 1:
                raise ValueError(f"position {id!r} not found")

    # --- fills ----------------------------------------------------------

    def save_fill(self, f: Fill) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO fills (id, client_order_id, market, symbol, side, price,"
                " quantity, fee_usd, timestamp, position_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f.id, f.client_order_id, f.market.value, f.symbol, f.side.value,
                    _dec(f.price), _dec(f.quantity), _dec(f.fee_usd), _ts(f.timestamp),
                    f.position_id,
                ),
            )

    def fills_between(self, start: datetime, end: datetime) -> list[Fill]:
        """Fills con start <= timestamp < end, en orden cronológico."""
        rows = self._conn.execute(
            "SELECT * FROM fills WHERE timestamp >= ? AND timestamp < ?"
            " ORDER BY timestamp, rowid",
            (_ts(start), _ts(end)),
        ).fetchall()
        return [_row_to_fill(r) for r in rows]

    # --- cursores -------------------------------------------------------

    def get_cursor(self, source: str) -> datetime | None:
        row = self._conn.execute("SELECT ts FROM cursors WHERE source = ?", (source,)).fetchone()
        return _parse_ts(row["ts"]) if row is not None else None

    def advance_cursor(self, source: str, ts: datetime) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO cursors (source, ts) VALUES (?, ?)"
                " ON CONFLICT(source) DO UPDATE SET ts = excluded.ts",
                (source, _ts(ts)),
            )

    # --- allowlist congelada --------------------------------------------

    def get_frozen_allowlist(self, session_id: str) -> frozenset[str] | None:
        row = self._conn.execute(
            "SELECT traders FROM frozen_allowlist WHERE session_id = ?", (session_id,)
        ).fetchone()
        return frozenset(json.loads(row["traders"])) if row is not None else None

    def freeze_allowlist(self, session_id: str, traders) -> None:
        """Escritura única por sesión. ValueError si ya estaba congelada."""
        payload = json.dumps(sorted(set(traders)))
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO frozen_allowlist (session_id, traders) VALUES (?, ?)",
                    (session_id, payload),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"allowlist already frozen for session {session_id!r}") from exc

    # --- estado persistente del bot ---------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def set_state(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- intenciones de orden (idempotencia) -----------------------------

    def reserve_intent(
        self, client_order_id: str, payload: dict, now: datetime | None = None
    ) -> bool:
        """Reserva el identificador antes de salir a la red (o re-reserva uno FAILED).

        Se apoya en la PRIMARY KEY: intenta el INSERT y devuelve False si ya
        existía. Un SELECT previo dejaría una ventana de carrera.
        """
        created = _ts(now if now is not None else datetime.now(timezone.utc))
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO order_intents (client_order_id, status, payload, created_at)"
                    " VALUES (?,?,?,?)",
                    (
                        client_order_id, IntentStatus.RESERVED.value,
                        json.dumps(payload, sort_keys=True, default=str), created,
                    ),
                )
        except sqlite3.IntegrityError:
            # Un FAILED significa que sabemos que la orden NO se ejecutó: reintentar
            # con el mismo identificador es seguro. RESERVED, SENT y CONFIRMED bloquean.
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE order_intents SET status = ?, payload = ?, created_at = ?,"
                    " error = NULL WHERE client_order_id = ? AND status = ?",
                    (IntentStatus.RESERVED.value, json.dumps(payload, sort_keys=True, default=str),
                     created, client_order_id, IntentStatus.FAILED.value),
                )
            return cur.rowcount == 1
        return True

    def _transition_intent(
        self, client_order_id: str, new: IntentStatus, allowed_from: tuple[IntentStatus, ...],
        error: str | None = None,
    ) -> None:
        marks = ",".join("?" * len(allowed_from))
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE order_intents SET status = ?, error = ?"
                f" WHERE client_order_id = ? AND status IN ({marks})",
                (new.value, error, client_order_id, *(s.value for s in allowed_from)),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    f"intent {client_order_id!r} not found or cannot move to {new.value}"
                    f" (allowed from: {', '.join(s.value for s in allowed_from)})"
                )

    def mark_intent_sent(self, client_order_id: str) -> None:
        """RESERVED -> SENT. ValueError si no existe o no estaba RESERVED."""
        self._transition_intent(client_order_id, IntentStatus.SENT, (IntentStatus.RESERVED,))

    def mark_intent_confirmed(self, client_order_id: str, fill: Fill) -> None:
        """RESERVED|SENT -> CONFIRMED. El fill debe pertenecer a esta orden."""
        if fill.client_order_id != client_order_id:
            raise ValueError(
                f"fill {fill.id!r} belongs to {fill.client_order_id!r}, not {client_order_id!r}"
            )
        self._transition_intent(
            client_order_id, IntentStatus.CONFIRMED, (IntentStatus.RESERVED, IntentStatus.SENT)
        )

    def mark_intent_failed(self, client_order_id: str, error: str) -> None:
        """RESERVED|SENT -> FAILED, con el motivo."""
        self._transition_intent(
            client_order_id, IntentStatus.FAILED,
            (IntentStatus.RESERVED, IntentStatus.SENT), error,
        )

    def unconfirmed_intents(self) -> list[OrderIntent]:
        """Intenciones RESERVED o SENT (resultado incierto), por antigüedad."""
        rows = self._conn.execute(
            "SELECT * FROM order_intents WHERE status IN (?, ?) ORDER BY created_at, rowid",
            (IntentStatus.RESERVED.value, IntentStatus.SENT.value),
        ).fetchall()
        return [
            OrderIntent(
                client_order_id=r["client_order_id"],
                status=IntentStatus(r["status"]),
                payload=json.loads(r["payload"]),
                created_at=_parse_ts(r["created_at"]),
                error=r["error"],
            )
            for r in rows
        ]
