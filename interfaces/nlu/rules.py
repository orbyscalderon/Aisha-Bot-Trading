"""Intérprete de comandos exactos: sin LLM, sin claves, sin coste y sin red.

Es la base del control por chat (fase 6 del diseño) y sigue siendo útil después de añadir
lenguaje natural: cuando algo falla, permite distinguir si falló el bot o la interpretación.
Por eso el dispatcher lo usa SIEMPRE primero y solo recurre al LLM si aquí sale ``UNKNOWN``.

Entiende el nombre del comando en inglés o en castellano, con o sin ``/`` delante, en cualquier
combinación de mayúsculas, y tolera el sufijo ``@NombreDelBot`` que Telegram añade en los grupos.
"""

from __future__ import annotations

from interfaces.commands import MAX_ARG_LENGTH, Command, CommandError, CommandName

#: Nombre escrito -> comando. En castellano además del inglés porque es la lengua en que se usa.
VOCABULARY: dict[str, CommandName] = {
    "status": CommandName.STATUS, "estado": CommandName.STATUS,
    "positions": CommandName.POSITIONS, "posiciones": CommandName.POSITIONS,
    "pnl": CommandName.PNL, "resultado": CommandName.PNL,
    "pause": CommandName.PAUSE, "pausa": CommandName.PAUSE, "pausar": CommandName.PAUSE,
    "resume": CommandName.RESUME, "reanudar": CommandName.RESUME, "continuar": CommandName.RESUME,
    "stop": CommandName.STOP, "parar": CommandName.STOP, "alto": CommandName.STOP,
    "help": CommandName.HELP, "ayuda": CommandName.HELP, "start": CommandName.HELP,
}


class RulesParser:
    """Traduce comandos exactos. Nunca lanza: lo que no encaja es ``UNKNOWN``."""

    name = "rules"

    def parse(self, text: str) -> Command:
        if not isinstance(text, str):
            return Command(CommandName.UNKNOWN)
        limpio = text.strip()
        if not limpio:
            return Command(CommandName.UNKNOWN)

        partes = limpio.split()
        cabeza = partes[0].lstrip("/")
        cabeza = cabeza.split("@", 1)[0].lower()  # '/status@AishaBot' en los grupos de Telegram
        nombre = VOCABULARY.get(cabeza)
        if nombre is None:
            return self._unknown(limpio)

        try:
            if nombre is CommandName.PNL and len(partes) > 1:
                return Command(nombre, {"period": partes[1]})
            # Los demás no llevan argumentos: '/pausa por favor' es una pausa, y las palabras
            # sobrantes se ignoran en vez de convertirse en parámetros que nadie declaró.
            return Command(nombre)
        except CommandError:
            # Un argumento que el vocabulario rechaza (demasiado largo, por ejemplo) no puede
            # tumbar el bucle de escucha: se degrada a UNKNOWN y el usuario recibe la ayuda.
            return self._unknown(limpio)

    @staticmethod
    def _unknown(text: str) -> Command:
        return Command(CommandName.UNKNOWN, {"text": text[:MAX_ARG_LENGTH]})
