import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


#: Todo lo que el bot lee del entorno o del `.env`. La lista es explícita a propósito: una
#: clave nueva que no aparezca aquí hará fallar un test en la máquina de quien la tenga puesta
#: y pasar en el resto, que es la peor forma de enterarse.
SECRETOS = (
    "HELIUS_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "LLM_API_KEY",
    "EXCHANGE_API_KEY",
    "EXCHANGE_API_SECRET",
    "SOLANA_PRIVATE_KEY",
    "SOLANA_RPC_URL",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "TWILIO_TO_NUMBER",
)


@pytest.fixture(autouse=True)
def entorno_aislado(monkeypatch):
    """Ningún test ve las claves REALES de quien lo ejecuta, ni las que dejó el test anterior.

    Hacen falta las dos mitades:

    - **Borrar antes.** Si la suite leyera el `.env` de verdad, un test de "falta el token"
      pasaría en integración y fallaría en el portátil del que lo tiene configurado. Un test
      que depende de la máquina no prueba nada.
    - **Restaurar después.** Los comandos del CLI vuelcan el `.env` en ``os.environ``, que es
      global al proceso. Sin restaurar, lo que carga un test se lo encuentra el siguiente y
      la suite pasa a depender del ORDEN, que es un fallo que aparece y desaparece solo.

    ``monkeypatch`` deshace ambas cosas al terminar cada test.
    """
    for nombre in SECRETOS:
        monkeypatch.delenv(nombre, raising=False)
    # Deshace también cualquier variable que el propio test (o el CLI) añada después.
    original = dict(os.environ)
    yield
    for nombre in set(os.environ) - set(original):
        os.environ.pop(nombre, None)
