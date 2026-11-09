# Aisha Bot Trading

Bot de copy-trading multimercado.

Copia las compras de traders que sigues, con límites de riesgo estrictos, y te deja
**probar la estrategia sobre datos pasados antes de arriesgar nada**.

> **ADVERTENCIA: el modo real NO existe todavía.**
> Este bot no envía órdenes reales a ningún exchange. Solo hace backtest y ejecución
> **simulada** (paper). Con `mode: live`, `run` se niega a arrancar y termina con error:
> la ejecución real es una fase posterior del desarrollo (el "plan 3" del plan de trabajo interno), que se construye a propósito después de probar bien el resto. No hay ninguna forma de que este código mueva dinero.

## Qué hace y qué no hace

Hace:
- **Backtest**: reproduce señales pasadas con el mismo motor de decisión que usaría en
  vivo y te da PnL, drawdown, tasa de acierto, cierres por motivo y un informe HTML.
  Es lo único verdaderamente útil hoy.
- **Ejecución simulada (`run`)**: lee señales de un CSV, consulta cotizaciones reales de
  un exchange (vía ccxt, solo datos públicos) y "ejecuta" las órdenes en papel, con
  deslizamiento y comisión simulados.
- Aplica límites de riesgo (por token, por día, pérdida diaria máxima, posiciones
  abiertas), stop-loss, protecciones (cooldown, racha de stops, drawdown, exposición por
  grupo) y un kill switch.

- **Copia wallets de Solana EN VIVO** (`run --solana-wallet`): lee los swaps de la wallet en
  cada ciclo vía Helius y los convierte en señales, sin pasar por ningún CSV.
- **Busca y criba traders**: `discover-solana-wallets` saca direcciones que están operando
  ahora; `screen-solana-wallets` mide cuáles son traders de verdad y cuáles son
  infraestructura.
- **Se maneja por Telegram**: `/estado`, `/posiciones`, `/pausa`, `/stop`, en español o en
  inglés. Con una clave de Claude entiende además frases libres.
- **Arranca solo con el equipo** (`scripts/instalar-servicio.ps1`, Windows).

No hace:
- Ejecución real (plan 3). Ni conecta claves de trading ni envía órdenes.
- Garantizar nada: un backtest rentable no predice resultados futuros.
- Decirte a quién copiar. La criba **descarta** con seguridad; que una wallet la pase solo
  significa que merece un backtest, no que ganes dinero copiándola.

## Instalación

Requiere Python 3.11 o superior.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; en Linux/macOS: source .venv/bin/activate
pip install -e .
pip install pytest            # solo para ejecutar los tests
```

Esto instala el comando `copybot` (también puedes usar `python cli.py`).
Los ejemplos de abajo usan `copybot`.

## Configuración

Copia `config/config.example.yaml` a `config/paper.yaml` y ajústalo (en el repositorio hay un
`config/paper.yaml` ya hecho, con límites conservadores). Las secciones:

| Sección | Qué controla |
|---|---|
| `mode` | `paper` (simulado). `live` existe en el esquema pero `run` lo rechaza. |
| `sizing` | Cuánto invertir por señal: `flat` (importe fijo), `mirror` (porcentaje del trader con tope) o `risk` (porcentaje de riesgo con stop). |
| `exits` | Stop-loss, take-profit, time-stop, trailing y la salida primaria (`copy_sell`, `take_profit` o `time_stop`). **En el plan 1 `copy_sell` no está conectada**: las salidas efectivas son el stop-loss y el time-stop, y el bot lo avisa al arrancar. |
| `risk` | Máximo por token, posiciones abiertas, gasto diario, pérdida diaria y techo duro por orden. |
| `cadence` | `entry_cron` (cuándo se buscan entradas, UTC) y `exit_interval_seconds` (cada cuánto se vigilan las salidas). |
| `protections` | `cooldown_minutes`, `stop_loss_streak`, `max_drawdown_pct`, `max_usd_per_group`. **Un 0 desactiva la protección.** |
| `groups` | Símbolos correlacionados, para el límite por grupo. |
| `execution` | Deslizamiento y comisión simulados por mercado (hay valores por defecto). |
| `allowlist` | Traders permitidos (vacío: los que aparezcan en el CSV). |

Una clave mal escrita en cualquier sección es un error, no se ignora.

**Protecciones.** Si omites la sección `protections` el bot corre sin ninguna. En `paper`
y en backtest eso solo genera un aviso al arrancar; en `live` la sección es obligatoria
(puedes escribir ceros, pero tiene que ser una decisión escrita).

## Comandos

```bash
copybot backtest --config config/paper.yaml --signals senales.csv --quotes cotizaciones.csv --report informe.html
copybot run      --config config/paper.yaml --solana-wallet <dirección> [--once]
copybot run      --config config/paper.yaml --signals senales.csv [--once]
copybot status   --config config/paper.yaml
copybot positions --config config/paper.yaml
copybot stop
```

Para Solana y Telegram hay comandos propios, explicados más abajo:
`discover-solana-wallets`, `screen-solana-wallets`, `fetch-solana-signals`,
`fetch-solana-quotes` y `telegram-chat-id`.

- `backtest`: reproduce el pasado. Imprime métricas, cierres por motivo y las
  **posiciones que siguen abiertas al final con su PnL latente** (las métricas solo
  cuentan lo cerrado; fíjate también en esa línea). `--report` escribe el informe HTML de
  quantstats. `--step-minutes` (60 por defecto) fija el paso del reloj simulado.
- `run`: ejecución simulada continua. Necesita **una** fuente de señales: `--solana-wallet`
  (en vivo, repetible) o `--signals` (un CSV estático), nunca las dos a la vez. Lanza un
  ciclo de entrada según `cadence.entry_cron`
  y otro de salida cada `exit_interval_seconds`; nunca se solapan, y si el equipo se
  suspende se ejecuta un solo ciclo atrasado al despertar. `--once` ejecuta un ciclo de
  cada y termina. `--exchange` elige el exchange de las cotizaciones (por defecto
  `binance`), `--capital` el capital simulado (10000 USD).
- `status` / `positions`: modo, estado del kill switch y posiciones abiertas.
- `stop`: crea el fichero `STOP` en la carpeta del bot (`--root`). Con él, no se abren
  posiciones nuevas; las salidas siguen. Borra el fichero para reanudar.

Todos aceptan `--root` (carpeta de la base de datos `paper.db` y del fichero `STOP`;
por defecto la actual). Los errores de configuración se muestran como mensaje.

### Formatos de los CSV

Señales (`--signals`), `side` es `buy` o `sell`; `amount_usd` puede ir vacío:

```
timestamp,trader_id,symbol,side,amount_usd
2026-01-01T10:00:00+00:00,trader1,BTC/USDT,buy,100
```

Cotizaciones (`--quotes`), una fila por símbolo y momento:

```
timestamp,symbol,price
2026-01-01T10:00:00+00:00,BTC/USDT,100
```

Las cotizaciones las tienes que aportar tú: el bot no las descarga. Una forma sencilla es
exportar velas históricas del exchange (por ejemplo con `ccxt`: `exchange.fetch_ohlcv(symbol,
'1h')`, usando el cierre de cada vela como `price`) o bajarlas de la web del exchange y
convertirlas a ese formato. Cuanto más fino el intervalo, más fiel el stop-loss simulado.

Las fechas sin zona horaria se interpretan como UTC. El backtest simula un solo mercado
por ejecución.

## Claves: el fichero `.env`

Las claves van en un fichero `.env` en la carpeta del bot (la de `--root`), **nunca en el
YAML** ni en el repositorio. Está en `.gitignore`.

```
HELIUS_API_KEY=...                # leer wallets de Solana
TELEGRAM_BOT_TOKEN=...            # avisos y control por chat
TELEGRAM_ALLOWED_CHAT_IDS=123456  # QUIÉN puede dar órdenes al bot
LLM_API_KEY=...                   # opcional: entender frases libres
```

Dos reglas del cargador, por si sorprenden:

- **Lo que ya esté en el entorno gana al fichero.** Un `.env` olvidado en el disco no puede
  silenciar lo que un servicio configuró de forma explícita.
- **Un `#` solo es comentario al principio de la línea.** Recortar en silencio el final de
  un secreto da un "token inválido" imposible de diagnosticar.

## Control por Telegram

Define `TELEGRAM_BOT_TOKEN` y `TELEGRAM_ALLOWED_CHAT_IDS` y el bot escucha. Entiende
`/estado`, `/posiciones`, `/pnl`, `/pausa`, `/reanudar`, `/stop` y `/ayuda`, en español y en
inglés. Con `LLM_API_KEY` entiende además frases sueltas, pero **el modelo solo traduce a un
comando de esa lista cerrada: nunca ejecuta lo que lea**. Es la defensa contra que alguien le
escriba instrucciones al bot.

La autorización **falla cerrada**: sin `TELEGRAM_ALLOWED_CHAT_IDS` no hay nadie autorizado, y
a un chat no autorizado no se le responde nada (responder ya confirmaría que el bot existe).

El `chat_id` no es el nombre del bot: es el número de **tu** conversación con él, y Telegram
solo lo revela después de que le escribas. Para sacarlo:

```bash
copybot telegram-chat-id        # manda antes cualquier mensaje al bot
```

Todo mensaje empieza por `mode: paper` (o `mode: live`), para que nunca haya duda de en qué
modo estás. Sin token, los avisos salen por consola y el bot opera igual: quedarse sin operar
por no poder avisar sería dejar de hacer lo importante por no poder hacer lo accesorio.

## Copiar wallets de Solana

El orden importa, y los dos primeros pasos existen porque saltárselos sale caro: la primera
wallet que se probó parecía un trader y era infraestructura que pagaba comisiones ajenas.

```bash
# 1. ¿quién está operando ahora? (GeckoTerminal, gratis, sin clave)
copybot discover-solana-wallets --historial barridos.csv

# 2. ¿cuáles son traders de verdad? (Helius; descarta bots e infraestructura)
copybot fetch-quotes -s SOL/USDT --since 2026-11-01T00:00:00+00:00 --out sol.csv
copybot screen-solana-wallets -w <dirección> --since 2026-11-01T00:00:00+00:00 --sol-quotes sol.csv

# 3. ¿habría ganado dinero copiarla? (eso solo lo dice el backtest)
copybot fetch-solana-signals -w <dirección> --since ... --out senales.csv --sol-quotes sol.csv
copybot fetch-solana-quotes --signals senales.csv --since ... --out precios.csv
copybot backtest --signals senales.csv --quotes precios.csv

# 4. copiarla en vivo
copybot run --solana-wallet <dirección>
```

### Un solo barrido selecciona bots

Esto no es un defecto de los umbrales, es el método: el barrido mira los últimos ~300
intercambios de cada pool, que en los activos son **minutos**. Quien opera dos veces al día no
aparece casi nunca; quien opera sin parar aparece siempre.

Lo que sí funciona es repetir el barrido durante días contra el mismo historial:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar-barrido.ps1
```

Registra un barrido cada 4 horas (solo lectura: no opera, no toca la base de datos del bot y
no gasta peticiones de Helius). Pasados unos días:

```bash
copybot discover-solana-wallets --historial datos/barridos.csv --min-barridos 8 --out candidatas.txt
```

"Visto en 8 barridos de 4 días" no se puede fingir operando rápido: hay que seguir ahí mañana.

**Lo que la criba mide** (todo sale de swaps reales, nada se estima): tamaño mediano de las
operaciones, cuántos tokens distintos toca, compras frente a ventas, ritmo de operaciones,
qué fracción de sus swaps son comisiones de otros, y qué fracción mueve varias monedas a la
vez por ambos lados — esto último delata a los enrutadores y arbitrajistas, que parecen
traders si solo miras las pocas operaciones que se les entienden.

**Lo que la criba NO mide**: si copiarla gana dinero. Eso depende de tus umbrales (un trader
excelente con posiciones de seis meses es inútil con un time-stop de 72 h) y lo contesta el
backtest.

## Arranque automático (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar-servicio.ps1 -SolanaWallet <dirección>
```

Registra una tarea programada que arranca el bot al iniciar sesión, lo reinicia si se cae y
escribe todo en `logs\bot.log`. Comprueba **antes de registrar nada** que existen el entorno
virtual, la configuración y la clave de Helius, y se niega si el YAML dice `mode: live`. Avisa
—sin impedir la instalación— si el bot va a arrancar mudo por faltarle Telegram.

Parada de emergencia: crea un fichero vacío llamado `STOP` en la carpeta del bot, o mándale
`/stop` por Telegram. Desinstalar: `scripts\desinstalar-servicio.ps1`.

## Probar un backtest rápido

1. Copia y ajusta `config/config.example.yaml`.
2. Prepara los dos CSV de arriba con tus señales y las cotizaciones del periodo.
3. `copybot backtest --config config/config.yaml --signals senales.csv --quotes cotizaciones.csv --report informe.html`

### Ejemplo de salida de `backtest`

```
mode: paper | backtest 2026-01-01T10:00:00+00:00 -> 2026-01-01T13:00:00+00:00 (paso 60 min)
mode: paper | PnL realizado: -30.0 USD | drawdown máx.: 30.0 USD
mode: paper | tasa de acierto: 0 | expectativa: -30.0 USD | Sharpe: 0
mode: paper | cierres por motivo:
  STOP_LOSS: 1
mode: paper | Posiciones abiertas al final: 0   (PnL latente: 0.00 USD)
```

(Las cifras son ilustrativas.) Mira siempre la línea de posiciones abiertas: las métricas
solo cuentan lo cerrado, y unas pérdidas latentes grandes no aparecerían en ellas.

## Tests

```bash
pytest
```
