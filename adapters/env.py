"""Lectura del fichero ``.env``.

POR QUÉ EXISTE. Durante un tiempo solo ``HELIUS_API_KEY`` se leía del ``.env``; el resto de
claves (las tres de Telegram, la del modelo) se buscaban únicamente en el entorno del proceso.
Desde una terminal donde alguien las había exportado a mano funcionaba, así que el fallo no se
veía. Desde la tarea programada de Windows no hay nadie que exporte nada: el bot arrancaba
operando pero mudo y sin control por chat, y lo hacía EN SILENCIO, porque quedarse sin chat
está diseñado para no impedir operar. Un fallo que no se nota es peor que uno que revienta.

DOS REGLAS QUE NO SON OBVIAS:

1. **El entorno real gana al fichero.** Si una variable ya viene del proceso, el ``.env`` no la
   pisa. Al revés sería un fichero olvidado en el disco silenciando la configuración explícita
   de un servicio o de un CI, que es justo donde más caro sale.

2. **No se recortan comentarios al final de un valor.** Es tentador tratar ``clave=v # nota``
   como ``v``, pero un secreto puede contener casi cualquier cosa, y recortarlo en silencio
   da un "token inválido" imposible de diagnosticar. Aquí un ``#`` solo es comentario cuando
   empieza la línea.

Los valores NUNCA se devuelven juntos ni se registran: las funciones públicas informan de los
NOMBRES cargados, nunca de su contenido.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, MutableMapping

#: Nombre del fichero, junto a la raíz del bot (la de ``--root``).
DOTENV_FILENAME = ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """Pares ``NOMBRE=valor`` de un ``.env``, ya limpios.

    Tolera lo que traen los ficheros reales: BOM de Windows, finales de línea CRLF, líneas en
    blanco, comentarios, ``export`` delante y comillas alrededor del valor. Una línea que no
    se entiende se ignora en vez de romper: un ``.env`` a medio escribir no debe impedir
    arrancar con las claves que sí están bien.
    """
    valores: dict[str, str] = {}
    for linea in text.replace("﻿", "").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if linea.startswith("export "):
            linea = linea[len("export "):].lstrip()
        nombre, sep, bruto = linea.partition("=")
        nombre = nombre.strip()
        if not sep or not nombre:
            continue
        bruto = bruto.strip()
        if len(bruto) >= 2 and bruto[0] == bruto[-1] and bruto[0] in "'\"":
            bruto = bruto[1:-1]
        else:
            bruto = bruto.strip()
        valores[nombre] = bruto
    return valores


def read_dotenv(root: Path | str) -> dict[str, str]:
    """El ``.env`` de ``root``, o vacío si no existe o no se puede leer.

    No lanza: que falte el fichero es lo normal cuando todo viene del entorno.
    """
    ruta = Path(root) / DOTENV_FILENAME
    try:
        texto = ruta.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    return parse_dotenv(texto)


def load_dotenv(
    root: Path | str,
    env: MutableMapping[str, str] | None = None,
    *,
    only: Iterable[str] | None = None,
) -> list[str]:
    """Vuelca el ``.env`` de ``root`` en ``env`` y devuelve los NOMBRES cargados.

    No pisa lo que ya trae el entorno (ver regla 1 de la cabecera) ni carga valores vacíos: en
    un ``.env`` una clave sin rellenar significa "no tengo esto", y meterla como cadena vacía
    haría que un ``if not token`` siguiera siendo falso por accidente en otro sitio.

    ``only`` limita qué nombres se cargan. Sin él se cargan todos.
    """
    destino = os.environ if env is None else env
    permitidos = None if only is None else set(only)
    cargados: list[str] = []
    for nombre, valor in read_dotenv(root).items():
        if permitidos is not None and nombre not in permitidos:
            continue
        if not valor:
            continue
        if (destino.get(nombre) or "").strip():
            continue
        destino[nombre] = valor
        cargados.append(nombre)
    return cargados
