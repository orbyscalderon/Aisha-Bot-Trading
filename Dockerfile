# Imagen del bot. Pensada para un contenedor SIEMPRE ENCENDIDO, no para un cron:
# el bot usa apscheduler dentro del proceso y revisa las salidas cada 60 s. Un
# proveedor que duerma el proceso por inactividad NO sirve: dormido no se dispara
# un stop-loss, y la posicion sigue abierta cayendo sin que nadie la mire.

FROM python:3.12-slim

# PYTHONUNBUFFERED: sin esto Python almacena la salida y `docker logs` no muestra
# nada durante minutos. Un bot que parece mudo es indistinguible de un bot caido.
# TZ=UTC: el cron de entrada ("0 * * * *") y las marcas de las velas se interpretan
# en la zona del contenedor. Fijarla evita que el bot opere a otra hora al cambiar
# de maquina, y que el horario de verano mueva las aperturas una hora dos veces al ano.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

# Las dependencias, en su propia capa y leidas del pyproject: UNA sola fuente de
# verdad. Repetir la lista aqui haria que anadir una dependencia al pyproject dejara
# la imagen sin ella, y el bot reventaria con un ImportError ya en la nube, no en los
# tests. Esta capa solo se reconstruye si cambia el pyproject, no al tocar codigo.
COPY pyproject.toml ./
RUN python -c "\
import tomllib, subprocess, sys; \
deps = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; \
subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*deps])"

COPY cli.py ./
COPY core/ ./core/
COPY ports/ ./ports/
COPY adapters/ ./adapters/
COPY storage/ ./storage/
COPY backtest/ ./backtest/
COPY interfaces/ ./interfaces/
COPY config/ ./config/

# /datos es el VOLUMEN: ahi viven la base de datos (paper.db), el fichero STOP del
# kill switch y el historial de barridos. Si no se monta un volumen de verdad, el
# contenedor arranca cada vez con las posiciones abiertas perdidas y sin auditoria.
RUN mkdir -p /datos && \
    useradd --create-home --uid 10001 bot && \
    chown -R bot:bot /datos /app
VOLUME ["/datos"]

# Sin root: si algo del bot se compromete, no manda en el contenedor.
USER bot

# --root /datos manda la base de datos y el STOP al volumen, no a la imagen.
# Los secretos (HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, EXCHANGE_API_*) se pasan como
# variables de entorno del proveedor. NUNCA con COPY ni con ENV en este fichero:
# un ENV con un secreto queda grabado en la imagen y lo lee `docker history`.
ENTRYPOINT ["python", "-m", "cli"]
CMD ["run", "--config", "config/paper.yaml", "--root", "/datos"]
