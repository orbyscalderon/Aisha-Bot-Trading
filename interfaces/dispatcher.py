"""Dispatcher: autoriza al remitente, ejecuta el comando y deja constancia.

Es la única pieza de la capa conversacional que toca el estado del bot. El intérprete solo
produce intenciones; aquí se decide si esa intención se ejecuta.

Decisiones:

- **Falla cerrado.** Una allowlist vacía no autoriza a nadie. Si significara "todos", un ``.env``
  a medio rellenar abriría el bot a cualquiera que acertase el nombre del bot de Telegram.
- **A un desconocido no se le responde.** Ni un error, ni un "no autorizado": silencio. Contestar
  cualquier cosa confirma que el bot existe y que ese canal lleva a algún sitio (diseño §10.4).
  Sí se anota el intento —saberlo importa— pero sin guardar el texto del mensaje: sería almacenar
  basura de desconocidos, y es por donde entraría cualquier cosa rara al log.
- **Solo se audita lo que cambia el estado.** Registrar cada ``/status`` llenaría el log de ruido
  y enterraría lo que de verdad hay que poder reconstruir después.
- **Ningún fallo de una consulta puede tumbar el bucle de escucha.** Si un informe revienta, el
  usuario recibe un mensaje de error; si en cambio la excepción subiera, cualquiera podría apagar
  el bot con un mensaje que provocara el fallo.
- **El kill switch se activa pero no se desactiva.** La respuesta dice explícitamente que hay que
  ir a la terminal, para que nadie se quede esperando a que un ``/resume`` lo deshaga.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from core.safety import activate_kill_switch, pause, resume
from interfaces.commands import MUTATING, Command, CommandName
from interfaces.nlu.base import IntentParser

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Comandos:\n"
    "  /status      modo, posiciones abiertas y candados activos\n"
    "  /positions   posiciones abiertas con su resultado latente\n"
    "  /pnl [7d]    resultado realizado del periodo\n"
    "  /pause       detiene las APERTURAS (las salidas siguen)\n"
    "  /resume      reanuda las aperturas\n"
    "  /stop        kill switch: solo se deshace desde la terminal\n"
    "  /help        esto\n"
    "\n"
    "También en castellano: estado, posiciones, pausa, reanudar, parar, ayuda.\n"
    "Pasar a dinero real NO se puede por chat, a propósito."
)


class Reports(Protocol):
    """Las consultas de solo lectura. El dispatcher no sabe de dónde salen los datos."""

    def status(self) -> str: ...
    def positions(self) -> str: ...
    def pnl(self, period: str | None) -> str: ...


class _Audit(Protocol):
    def record(self, event: str, payload: dict) -> Any: ...


@dataclass
class Dispatcher:
    root: Path
    authorized: frozenset[str]
    parsers: Sequence[IntentParser]
    reports: Reports
    audit: _Audit | None = None

    def handle(self, sender: Any, text: str) -> str | None:
        """Procesa un mensaje. Devuelve la respuesta, o ``None`` si no hay que contestar."""
        quien = str(sender)
        if quien not in self.authorized:
            # Sin texto: no se guarda el contenido de mensajes de desconocidos.
            self._record("chat_unauthorized", {"sender": quien})
            return None

        command = self._parse(text)
        if command.name in MUTATING:
            self._record("chat_command", {"sender": quien, "command": command.name.value})
        try:
            return self._execute(command, quien)
        except Exception as exc:  # noqa: BLE001 - un fallo no puede matar el bucle de escucha
            log.exception("fallo ejecutando %s", command.name.value)
            return f"error ejecutando {command.name.value}: {type(exc).__name__}"

    # --- interno ---

    def _parse(self, text: str) -> Command:
        """Primero las reglas; el LLM solo entra con lo que las reglas no entienden.

        Así un comando exacto nunca depende de una llamada de red, y el coste del intérprete se
        paga solo cuando hace falta.
        """
        último = Command(CommandName.UNKNOWN)
        for parser in self.parsers:
            try:
                command = parser.parse(text)
            except Exception:  # noqa: BLE001 - un intérprete roto degrada, no tumba
                log.exception("el intérprete %s falló", getattr(parser, "name", "?"))
                continue
            if command.name is not CommandName.UNKNOWN:
                return command
            último = command
        return último

    def _execute(self, command: Command, quien: str) -> str:
        name = command.name
        if name is CommandName.STATUS:
            return self.reports.status()
        if name is CommandName.POSITIONS:
            return self.reports.positions()
        if name is CommandName.PNL:
            return self.reports.pnl(command.args.get("period"))
        if name is CommandName.PAUSE:
            pause(self.root, f"por chat, remitente {quien}")
            return "En pausa: no se abren posiciones nuevas. Las salidas siguen vigiladas."
        if name is CommandName.RESUME:
            if resume(self.root):
                return "Reanudado: se vuelven a abrir posiciones."
            return "No estaba en pausa; sigue en marcha."
        if name is CommandName.STOP:
            activate_kill_switch(self.root, f"por chat, remitente {quien}")
            return (
                "KILL SWITCH ACTIVO: no se abrirá ninguna posición. Las salidas siguen.\n"
                "Esto NO se deshace por chat: hay que borrar el fichero STOP desde la terminal."
            )
        return HELP_TEXT

    def _record(self, event: str, payload: dict) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(event, payload)
        except Exception:  # noqa: BLE001 - no poder auditar no justifica dejar de responder
            log.exception("no se pudo auditar %s", event)
