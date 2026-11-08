# Ejemplos: datos reales de ejecuciones reales

Nada de esto es inventado ni generado: son las salidas de ejecutar el bot contra Binance y
contra Helius de verdad. Están aquí para que cualquiera pueda reproducir un backtest sin
credenciales y para dejar constancia de qué se ha probado.

## Cotizaciones

| Fichero | Qué es |
|---|---|
| `cotizaciones.csv` | Cuatro precios a mano, para el ejemplo mínimo del README |
| `cotizaciones_reales.csv` | Velas de 1 h de Binance (`fetch-quotes`) |
| `q3.csv` | Velas de 1 h de un trimestre completo |
| `sol_usdt.csv` | 955 velas de 1 h de SOL/USDT, 2026-09-30 → 2026-11-08. SOL pasó de 118,95 a 89,56 USD |

## Señales

| Fichero | Qué es |
|---|---|
| `senales.csv` | Dos señales a mano, para el ejemplo mínimo |
| `senales_demo.csv` | Señales de ejemplo del recorrido completo |
| `senales_q3.csv` | Señales del trimestre que acompaña a `q3.csv` |
| `senales_solana.csv` | **58 señales reales** leídas de una wallet de Solana vía Helius |

## Configuraciones y salidas

| Fichero | Qué es |
|---|---|
| `config_takeprofit.yaml`, `config_largo.yaml` | Dos calibraciones del mismo backtest, para comparar |
| `informe.html` | Informe de quantstats de un backtest |

## Lo que enseñan estos ficheros

**`senales_solana.csv` es el resultado de un error de diseño corregido.** La primera ejecución
contra Helius leyó 545 swaps y produjo **cero** señales: 442 se descartaban por no llevar
ninguna stablecoin. La suposición de partida —"los traders operan contra dólares"— es falsa en
Solana, donde se opera contra SOL. Con el precio de SOL de cada hora (`sol_usdt.csv`) el mismo
historial da 58 señales.

**La wallet de ese fichero no es un trader.** Se eligió solo para probar el lector. Al mirar los
datos resultó ser infraestructura: paga la comisión de swaps ajenos (77 casos) y sus propias
operaciones son de 0,005 SOL fijos, medio dólar. Sirve para verificar el lector, no para copiarla.

**Un backtest con buen número puede estar mintiendo.** Las dos configuraciones existen porque
alargar el time-stop de 72 h a 480 h dio la vuelta al resultado, de −8,64 a +5,54. Ese +5,54
está adornado por cuatro posiciones abiertas en pérdidas (−1,08 netos), y mover un parámetro
hasta que el número gusta es sobreajuste, no mejora.
