"""El puerto del intérprete: texto libre -> un Command del vocabulario cerrado.

Hay dos implementaciones intercambiables (diseño §10.5): ``rules.py``, que no usa ningún LLM y
entiende comandos exactos, y ``claude.py``, que entiende lenguaje natural. Se elige por
configuración. El contrato es el mismo y lo importante es lo que NO está en él: el intérprete no
recibe la base de datos, ni los adaptadores, ni las credenciales, ni nada que ejecute. Solo
devuelve una intención, que el dispatcher autoriza después.

Un intérprete NUNCA lanza una excepción por un texto raro: devuelve ``UNKNOWN``. Si un mensaje
mal formado pudiera tumbar el bucle de escucha, cualquiera podría apagarlo con un mensaje.
"""

from __future__ import annotations

from typing import Protocol

from interfaces.commands import Command


class IntentParser(Protocol):
    name: str

    def parse(self, text: str) -> Command:
        """Traduce ``text`` a un Command. Ante la duda, ``UNKNOWN``; nunca lanza."""
        ...
