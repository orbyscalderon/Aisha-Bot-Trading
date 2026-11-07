# Bot de copy-trading

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

No hace:
- Ejecución real (plan 3). Ni conecta claves de trading ni envía órdenes.
- Obtener señales por sí mismo: las señales se le dan en un CSV.
- Garantizar nada: un backtest rentable no predice resultados futuros.

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

Copia `config/config.example.yaml` a `config/config.yaml` y ajústalo. Las secciones:

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
copybot backtest --config config/config.yaml --signals senales.csv --quotes cotizaciones.csv --report informe.html
copybot run      --config config/config.yaml --signals senales.csv [--once]
copybot status   --config config/config.yaml
copybot positions --config config/config.yaml
copybot stop
```

- `backtest`: reproduce el pasado. Imprime métricas, cierres por motivo y las
  **posiciones que siguen abiertas al final con su PnL latente** (las métricas solo
  cuentan lo cerrado; fíjate también en esa línea). `--report` escribe el informe HTML de
  quantstats. `--step-minutes` (60 por defecto) fija el paso del reloj simulado.
- `run`: ejecución simulada continua. Lanza un ciclo de entrada según `cadence.entry_cron`
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

### Notificaciones

Por defecto los avisos salen por consola. Para recibirlos por Telegram define las
variables de entorno `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`. Todo mensaje empieza por
`mode: paper` (o `mode: live`), para que nunca haya duda de en qué modo estás.

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
