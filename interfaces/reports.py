"""Las tres consultas de solo lectura que responde el bot por chat.

Nada de aquí cambia nada. Se separa del dispatcher porque el dispatcher decide QUIÉN puede
preguntar y esto decide QUÉ se contesta; mezclarlos haría imposible probar la autorización sin
una base de datos detrás.

Dos reglas que se repiten en todos los informes:

- **Un precio que falta no es un error.** Si no se puede cotizar un símbolo, esa línea lo dice y
  las demás se calculan igual. Un informe que revienta entero porque falta un precio es un
  informe inútil justo cuando más falta hace.
- **Todo mensaje empieza por el modo.** La misma razón que en el notificador: creer que estás en
  simulado cuando operas con dinero real es la peor confusión posible.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from core.models import Mode, Position, Quote
from core.safety import is_kill_switch_active, is_paused, mode_prefix
from storage.repository import Repository

DEFAULT_PERIOD = "7d"
_PERIOD_RE = re.compile(r"^(\d{1,4})([hdw])$", re.IGNORECASE)
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
MAX_LISTED = 20  # más que esto no cabe cómodo en un chat


def parse_period(text: str | None) -> timedelta | None:
    """``'7d'`` -> 7 días. ``None`` si no se entiende (el informe lo dirá, no adivinará)."""
    match = _PERIOD_RE.match((text or DEFAULT_PERIOD).strip())
    if match is None:
        return None
    cantidad = int(match.group(1))
    if cantidad <= 0:
        return None
    return timedelta(**{_UNITS[match.group(2).lower()]: cantidad})


def _money(value: Decimal) -> str:
    return f"{value:+,.2f}".replace(",", " ")


class BotReports:
    """Informes leídos de la base de datos del bot y de los precios actuales."""

    def __init__(
        self,
        mode: Mode,
        root: Path,
        repo_factory: Callable[[], Repository],
        quote_fn: Callable[[str], Quote] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._mode = mode
        self._root = Path(root)
        self._repo_factory = repo_factory
        self._quote_fn = quote_fn
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- STATUS ---

    def status(self) -> str:
        repo = self._repo_factory()
        abiertas = repo.open_positions()
        pendientes = repo.unconfirmed_intents()
        candados = []
        if is_kill_switch_active(self._root):
            candados.append("KILL SWITCH (solo se quita desde la terminal)")
        if is_paused(self._root):
            candados.append("EN PAUSA")
        lineas = [
            mode_prefix(self._mode),
            f"candados: {', '.join(candados) if candados else 'ninguno'}",
            f"posiciones abiertas: {len(abiertas)}",
        ]
        if pendientes:
            # Una orden sin confirmar es ambigua: pudo ejecutarse o no. Se destaca.
            lineas.append(f"ÓRDENES SIN CONFIRMAR: {len(pendientes)} (requieren revisión)")
        latente = self._latent_total(abiertas)
        if latente is not None:
            lineas.append(f"resultado latente: {_money(latente)} USD")
        return "\n".join(lineas)

    # --- POSITIONS ---

    def positions(self) -> str:
        abiertas = self._repo_factory().open_positions()
        if not abiertas:
            return f"{mode_prefix(self._mode)}\nno hay posiciones abiertas"
        lineas = [mode_prefix(self._mode)]
        for p in abiertas[:MAX_LISTED]:
            precio = self._price(p.symbol)
            if precio is None:
                lineas.append(f"{p.symbol}  {p.amount_usd:,.2f} USD  (sin precio ahora mismo)")
            else:
                lineas.append(
                    f"{p.symbol}  {p.pct_change(precio):+.2f}%  "
                    f"{_money(p.unrealized_pnl_usd(precio))} USD"
                )
        if len(abiertas) > MAX_LISTED:
            lineas.append(f"... y {len(abiertas) - MAX_LISTED} más")
        return "\n".join(lineas)

    # --- PNL ---

    def pnl(self, period: str | None) -> str:
        ventana = parse_period(period)
        if ventana is None:
            return f"no entiendo el periodo {period!r}. Usa 24h, 7d o 4w."
        etiqueta = (period or DEFAULT_PERIOD).strip()
        cerradas = self._repo_factory().closed_positions_since(self._now() - ventana)
        if not cerradas:
            return f"{mode_prefix(self._mode)}\n{etiqueta}: ninguna posición cerrada"

        total = Decimal(0)
        por_motivo: dict[str, int] = {}
        ganadoras = 0
        for p in cerradas:
            if p.exit_price is None:
                continue
            resultado = p.unrealized_pnl_usd(p.exit_price)
            total += resultado
            if resultado > 0:
                ganadoras += 1
            motivo = p.exit_reason.value if p.exit_reason else "?"
            por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
        detalle = ", ".join(f"{m}: {n}" for m, n in sorted(por_motivo.items()))
        return "\n".join([
            mode_prefix(self._mode),
            f"{etiqueta}: {_money(total)} USD en {len(cerradas)} cierres",
            f"ganadoras: {ganadoras}/{len(cerradas)}",
            f"por motivo: {detalle}" if detalle else "",
        ]).rstrip()

    # --- precios ---

    def _price(self, symbol: str) -> Decimal | None:
        if self._quote_fn is None:
            return None
        try:
            return self._quote_fn(symbol).price
        except Exception:  # noqa: BLE001 - un precio que falta no rompe el informe entero
            return None

    def _latent_total(self, abiertas: list[Position]) -> Decimal | None:
        if self._quote_fn is None or not abiertas:
            return None
        total = Decimal(0)
        visto = False
        for p in abiertas:
            precio = self._price(p.symbol)
            if precio is not None:
                total += p.unrealized_pnl_usd(precio)
                visto = True
        return total if visto else None
