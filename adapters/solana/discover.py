"""Descubrimiento de wallets candidatas: de dónde sacar direcciones que examinar.

POR QUÉ EXISTE. La criba (``screener.py``) sabe decir si una wallet sirve, pero hay que darle
las direcciones. Hasta ahora salían de buscarlas a mano, y así se llegó a probar una wallet de
infraestructura creyendo que era un trader. Esto las saca de los intercambios reales: quién
está operando ahora mismo en los pools con liquidez de Solana.

CÓMO. GeckoTerminal publica los últimos intercambios de cada pool, y cada uno trae la dirección
que lo firmó (``tx_from_address``). Gratis, sin clave, la misma API que ya se usa para precios.

LO QUE ESTO **NO** ES. No es un veredicto ni un ranking de "mejores traders". Es una lista de
direcciones plausibles; quien decide es la criba, con datos de Helius. Dos razones concretas
por las que aquí no se puede juzgar:

- **La ventana es diminuta.** 300 intercambios del pool más activo pueden ser dos minutos.
  Calcular "operaciones al día" con eso daría miles, que es un artefacto de la división y no
  una medida. Por eso aquí NO se calcula ningún ritmo: ese número lo saca la criba leyendo el
  historial completo de la wallet.
- **Se ve una cara del pool, no la cartera.** Que alguien compre aquí no dice qué tiene ni
  cuánto aguanta las posiciones.

Dos filtros que sí se aplican, porque abaratan la criba sin fingir que juzgan:

- ``is_anomalous``: GeckoTerminal marca así los intercambios con precios imposibles. Un precio
  absurdo contamina cualquier mediana que se calcule después.
- **Tamaño mínimo e importe mediano**: una wallet que solo mueve monedas sueltas no se puede
  copiar aunque acierte, porque las comisiones se comen la operación. Es el mismo criterio que
  ``MIN_MEDIAN_USD`` en la criba, aplicado antes para no gastar peticiones de Helius en ella.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from adapters.solana.prices import GECKO_BASE, PAUSE_SECONDS, SolanaPriceError, _decimal, _get

logger = logging.getLogger(__name__)

#: Por debajo de esto un intercambio no dice nada de quien lo hizo: es polvo o una prueba.
DEFAULT_MIN_TRADE_USD = Decimal("100")
#: Aparecer en varios pools distingue a quien opera de quien pasaba por ahí una vez. Es el
#: equivalente barato de ``MIN_TOKENS`` de la criba, antes de gastar peticiones de Helius.
DEFAULT_MIN_POOLS = 2
#: Tope de pools a recorrer. Cada uno es una petición, y el plan gratuito da ~30 por minuto.
DEFAULT_MAX_POOLS = 20
#: Liquidez mínima del pool. En uno pequeño el precio se mueve con poco dinero, así que lo que
#: parece un trader acertando puede ser alguien moviendo su propio pool.
DEFAULT_MIN_LIQUIDITY_USD = Decimal("50000")


def _median(valores: Sequence[Decimal]) -> Decimal:
    if not valores:
        return Decimal(0)
    ordenados = sorted(valores)
    mitad = len(ordenados) // 2
    if len(ordenados) % 2:
        return ordenados[mitad]
    return (ordenados[mitad - 1] + ordenados[mitad]) / 2


@dataclass(frozen=True)
class WalletLead:
    """Una dirección vista operando. NO es un candidato aprobado: es uno por examinar."""

    wallet: str
    intercambios: int
    pools: tuple[str, ...]
    compras: int
    ventas: int
    mediana_usd: Decimal
    mayor_usd: Decimal
    primera: datetime | None = None
    ultima: datetime | None = None

    @property
    def tokens(self) -> int:
        """Pools distintos en los que se le ha visto. Cota INFERIOR de los tokens que toca."""
        return len(self.pools)


def _momento(valor: Any) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        texto = valor.replace("Z", "+00:00")
        momento = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if momento.tzinfo is None:
        return None
    return momento.astimezone(timezone.utc)


def fetch_top_pools(
    client: Any,
    *,
    max_pools: int = DEFAULT_MAX_POOLS,
    min_liquidity_usd: Decimal = DEFAULT_MIN_LIQUIDITY_USD,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[str, str, Decimal]]:
    """``(id, nombre, liquidez)`` de los pools con más volumen, de mayor a menor liquidez."""
    datos = _get(client, f"{GECKO_BASE}/pools", {"page": 1}, sleep)
    crudos = datos.get("data") if isinstance(datos, dict) else None
    pools: list[tuple[str, str, Decimal]] = []
    for p in crudos or []:
        if not isinstance(p, dict):
            continue
        bruto = str(p.get("id") or "")
        pid = bruto.split("_", 1)[1] if "_" in bruto else bruto
        atributos = p.get("attributes") if isinstance(p.get("attributes"), dict) else {}
        liquidez = _decimal(atributos.get("reserve_in_usd")) or Decimal(0)
        if not pid or liquidez < min_liquidity_usd:
            continue
        pools.append((pid, str(atributos.get("name") or pid), liquidez))
    pools.sort(key=lambda x: x[2], reverse=True)
    return pools[:max_pools]


def fetch_pool_trades(
    client: Any,
    pool_id: str,
    *,
    min_trade_usd: Decimal = DEFAULT_MIN_TRADE_USD,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Intercambios recientes de un pool, ya limpios.

    GeckoTerminal admite filtrar por importe, pero el filtro se repite aquí: un parámetro que
    la API ignore en silencio dejaría pasar polvo sin que nadie se entere.
    """
    datos = _get(
        client, f"{GECKO_BASE}/pools/{pool_id}/trades",
        {"trade_volume_in_usd_greater_than": str(min_trade_usd)}, sleep,
    )
    crudos = datos.get("data") if isinstance(datos, dict) else None
    limpios: list[dict[str, Any]] = []
    for t in crudos or []:
        if not isinstance(t, dict):
            continue
        a = t.get("attributes") if isinstance(t.get("attributes"), dict) else {}
        if a.get("is_anomalous") is True:
            continue   # precio imposible: contaminaría cualquier mediana posterior
        wallet = a.get("tx_from_address")
        usd = _decimal(a.get("volume_in_usd"))
        if not isinstance(wallet, str) or not wallet or usd is None or usd < min_trade_usd:
            continue
        limpios.append({
            "wallet": wallet,
            "usd": usd,
            "kind": str(a.get("kind") or ""),
            "momento": _momento(a.get("block_timestamp")),
        })
    return limpios


def discover_wallets(
    client: Any,
    *,
    max_pools: int = DEFAULT_MAX_POOLS,
    min_trade_usd: Decimal = DEFAULT_MIN_TRADE_USD,
    min_pools: int = DEFAULT_MIN_POOLS,
    min_liquidity_usd: Decimal = DEFAULT_MIN_LIQUIDITY_USD,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[WalletLead], dict[str, int]]:
    """``(direcciones a examinar, recuento de lo recorrido)``.

    Ordena por importe mediano descendente: no porque el más grande sea el mejor, sino porque
    es el que menos se parece a un bot de micro-operaciones, que es lo único que este paso
    puede distinguir. El veredicto lo da la criba.
    """
    pools = fetch_top_pools(
        client, max_pools=max_pools, min_liquidity_usd=min_liquidity_usd, sleep=sleep
    )
    if not pools:
        raise SolanaPriceError(
            f"ningún pool supera {min_liquidity_usd} USD de liquidez: baja --min-liquidez"
        )

    vistos: dict[str, dict[str, Any]] = {}
    recuento = {"pools": 0, "intercambios": 0, "pools_fallidos": 0}
    for indice, (pid, nombre, _liq) in enumerate(pools):
        if indice:
            sleep(PAUSE_SECONDS)   # ~30 peticiones/minuto en el plan gratuito
        try:
            trades = fetch_pool_trades(client, pid, min_trade_usd=min_trade_usd, sleep=sleep)
        except SolanaPriceError as exc:
            # Un pool que falla no puede tumbar el barrido: se cuenta y se sigue.
            logger.warning("pool %s (%s) no se pudo leer: %s", nombre, pid, exc)
            recuento["pools_fallidos"] += 1
            continue
        recuento["pools"] += 1
        recuento["intercambios"] += len(trades)
        for t in trades:
            registro = vistos.setdefault(t["wallet"], {
                "importes": [], "pools": set(), "compras": 0, "ventas": 0, "momentos": [],
            })
            registro["importes"].append(t["usd"])
            registro["pools"].add(nombre)
            if t["kind"] == "buy":
                registro["compras"] += 1
            elif t["kind"] == "sell":
                registro["ventas"] += 1
            if t["momento"] is not None:
                registro["momentos"].append(t["momento"])

    leads: list[WalletLead] = []
    for wallet, r in vistos.items():
        if len(r["pools"]) < min_pools:
            continue
        momentos = sorted(r["momentos"])
        leads.append(WalletLead(
            wallet=wallet,
            intercambios=len(r["importes"]),
            pools=tuple(sorted(r["pools"])),
            compras=r["compras"],
            ventas=r["ventas"],
            mediana_usd=_median(r["importes"]),
            mayor_usd=max(r["importes"]),
            primera=momentos[0] if momentos else None,
            ultima=momentos[-1] if momentos else None,
        ))
    leads.sort(key=lambda l: (l.mediana_usd, l.intercambios), reverse=True)
    return leads, recuento


def format_lead(l: WalletLead) -> str:
    """Una línea por dirección, pensada para leerla en una terminal."""
    return (
        f"  {l.wallet}  mediana {l.mediana_usd:>10.2f} USD  "
        f"{l.intercambios:>3} ops ({l.compras}c/{l.ventas}v)  "
        f"{l.tokens} pools: {', '.join(l.pools[:3])}"
    )
