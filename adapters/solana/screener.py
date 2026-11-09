"""Criba de wallets de Solana: medir si una wallet es un trader antes de copiarla.

POR QUÉ EXISTE. La primera wallet que se probó parecía un trader y resultó ser
infraestructura: pagaba la comisión de swaps ajenos (77 casos) y sus propias operaciones eran
de 0,005 SOL fijos, medio dólar. Nada de eso se ve en una lista de "mejores traders"; se ve
midiendo. Esto mide.

Lo que esto SÍ responde: ¿es un trader humano o un bot de infraestructura? ¿opera cantidades
que valga la pena copiar? ¿en cuántos tokens? ¿con qué frecuencia?

Lo que esto NO responde: ¿habría ganado dinero copiarlo? Esa pregunta es distinta y la contesta
el backtest, porque depende de TUS umbrales: un trader excelente con posiciones de seis meses es
inútil con un time-stop de 72 horas. Aquí no se inventa un PnL que el backtest calcula de verdad.

Todos los números salen de los swaps reales leídos por ``fetch_wallet_swaps``. Ninguno se estima.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from core.models import Side, Signal

#: Por debajo de esto, copiar la wallet no tiene sentido práctico aunque gane: las comisiones
#: se comen una operación de un dólar, y el tamaño delata un bot de micro-operaciones.
MIN_MEDIAN_USD = Decimal("20")
#: Una wallet que figura sobre todo como pagador de comisiones ajenas no está operando: está
#: dando servicio a otros. Copiar sus "operaciones" es copiar su fontanería.
MAX_FEE_PAYER_RATIO = Decimal("0.30")
MIN_SIGNALS = 10
MIN_TOKENS = 2
#: Por debajo de esta ventana, el ritmo de operaciones no significa nada: cuatro swaps en
#: dos minutos dan 'mil al dia', que es un artefacto de la division, no una medida.
MIN_WINDOW_DAYS = Decimal("0.04")  # ~1 hora
#: Por encima de esto no es alguien a quien copiar, es un bot de alta frecuencia. El ciclo
#: de entrada corre cada hora: de 200 operaciones al día se verían unas pocas, elegidas por
#: el azar de cuándo toca el cron, y copiarlas a destiempo es copiar el ruido sin la señal.
MAX_OPS_PER_DAY = Decimal("200")
#: El bot copia COMPRAS. Una wallet que casi solo vende no ofrece entradas que copiar, por
#: buena que sea: suele ser un distribuidor o un creador de mercado, no alguien con una tesis.
MIN_BUY_RATIO = Decimal("0.15")


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    ordenados = sorted(values)
    mitad = len(ordenados) // 2
    if len(ordenados) % 2:
        return ordenados[mitad]
    return (ordenados[mitad - 1] + ordenados[mitad]) / 2


@dataclass(frozen=True)
class WalletProfile:
    """Retrato medido de una wallet. Nada aquí es una opinión del modelo."""

    wallet: str
    swaps_leidos: int
    señales: int
    omitidos: dict[str, int] = field(default_factory=dict)
    compras: int = 0
    ventas: int = 0
    tokens: int = 0
    mediana_usd: Decimal = Decimal(0)
    menor_usd: Decimal = Decimal(0)
    mayor_usd: Decimal = Decimal(0)
    dias: Decimal = Decimal(0)
    primera: datetime | None = None
    ultima: datetime | None = None
    completa: bool = True

    @property
    def fee_payer_ratio(self) -> Decimal:
        """Fracción de swaps en los que la wallet solo pone la comisión de operaciones ajenas."""
        if self.swaps_leidos <= 0:
            return Decimal(0)
        return Decimal(self.omitidos.get("not_participant", 0)) / Decimal(self.swaps_leidos)

    @property
    def polvo_ratio(self) -> Decimal:
        """Fracción de swaps descartados por mover menos que el umbral de polvo."""
        if self.swaps_leidos <= 0:
            return Decimal(0)
        return Decimal(self.omitidos.get("below_dust", 0)) / Decimal(self.swaps_leidos)

    @property
    def buy_ratio(self) -> Decimal:
        """Fracción de operaciones que son compras: lo único que este bot sabe copiar."""
        if self.señales <= 0:
            return Decimal(0)
        return Decimal(self.compras) / Decimal(self.señales)

    @property
    def por_dia(self) -> Decimal | None:
        """Operaciones al día, o None si la ventana es demasiado corta para decir algo.

        Cuatro swaps en dos minutos dan '2.880 al día'. Ese número no describe a nadie: es
        un artefacto de dividir por una ventana diminuta, y leerlo como actividad real
        haría parecer frenético a quien solo fue leído durante dos minutos.
        """
        if self.dias < MIN_WINDOW_DAYS:
            return None
        return Decimal(self.señales) / self.dias

    def verdict(self) -> tuple[bool, list[str]]:
        """``(sirve como candidato, motivos del rechazo)``.

        Los umbrales son explícitos y conservadores: esto DESCARTA con seguridad, no promete
        que lo que pase sea bueno. Lo que pasa la criba va al backtest, que es quien responde
        a la pregunta que importa.
        """
        motivos: list[str] = []
        if not self.completa:
            # Un veredicto sobre una ventana truncada no es conservador, es falso: se leyeron
            # los ultimos minutos de una wallet muy activa y se juzgo como si fuera su
            # historia. Antes de certificar a nadie hay que leerle entero.
            motivos.append(
                "la lectura quedó incompleta (tope de peticiones): lo medido es solo el "
                "tramo más reciente, no su historial. Sube --max-requests y repite"
            )
        if self.señales < MIN_SIGNALS:
            motivos.append(
                f"solo {self.señales} operaciones valorables (mínimo {MIN_SIGNALS}): "
                "no hay historial del que fiarse"
            )
        if self.fee_payer_ratio > MAX_FEE_PAYER_RATIO:
            motivos.append(
                f"{self.fee_payer_ratio:.0%} de los swaps son ajenos y solo paga la comisión: "
                "es infraestructura, no un trader"
            )
        if self.mediana_usd < MIN_MEDIAN_USD:
            motivos.append(
                f"operación mediana de {self.mediana_usd:.2f} USD (mínimo {MIN_MEDIAN_USD}): "
                "las comisiones se comen una operación así"
            )
        if self.tokens < MIN_TOKENS:
            motivos.append(f"opera un solo token: no hay estrategia que copiar")
        if self.ventas == 0 and self.compras > 0:
            motivos.append("solo compra y nunca vende: no se puede medir si acierta")
        ritmo = self.por_dia
        if ritmo is not None and ritmo > MAX_OPS_PER_DAY:
            motivos.append(
                f"{ritmo:.0f} operaciones al día: es un bot de alta frecuencia, no alguien a "
                "quien copiar. El ciclo de entrada corre cada hora y solo vería unas pocas, "
                "elegidas por el azar de cuándo toca el cron"
            )
        if self.señales and self.buy_ratio < MIN_BUY_RATIO:
            motivos.append(
                f"solo el {self.buy_ratio:.0%} son compras ({self.compras} de {self.señales}): el bot copia ENTRADAS "
                "y aquí casi no hay; parece un distribuidor o un creador de mercado"
            )
        return (not motivos), motivos


def profile_wallet(
    wallet: str,
    signals: Sequence[Signal],
    swaps_leidos: int,
    omitidos: dict[str, int] | None = None,
    completa: bool = True,
) -> WalletProfile:
    """Retrato de una wallet a partir de sus señales ya leídas."""
    omitidos = dict(omitidos or {})
    importes = [s.trader_amount_usd for s in signals if s.trader_amount_usd is not None]
    momentos = sorted(s.timestamp for s in signals)
    dias = Decimal(0)
    if len(momentos) >= 2:
        span = (momentos[-1] - momentos[0]) / timedelta(days=1)
        dias = Decimal(str(round(span, 4)))
    return WalletProfile(
        wallet=wallet,
        swaps_leidos=swaps_leidos,
        señales=len(signals),
        omitidos=omitidos,
        compras=sum(1 for s in signals if s.side is Side.BUY),
        ventas=sum(1 for s in signals if s.side is Side.SELL),
        tokens=len({s.symbol for s in signals}),
        mediana_usd=_median(importes),
        menor_usd=min(importes) if importes else Decimal(0),
        mayor_usd=max(importes) if importes else Decimal(0),
        dias=dias,
        primera=momentos[0] if momentos else None,
        ultima=momentos[-1] if momentos else None,
        completa=completa,
    )


def format_profile(p: WalletProfile) -> str:
    """Retrato en texto, pensado para leerlo en una terminal o en un chat."""
    sirve, motivos = p.verdict()
    lineas = [
        f"{p.wallet}",
        f"  swaps leídos      {p.swaps_leidos}",
        f"  operaciones       {p.señales}  ({p.compras} compras / {p.ventas} ventas)",
        f"  tokens distintos  {p.tokens}",
        f"  tamaño mediano    {p.mediana_usd:.2f} USD   (de {p.menor_usd:.2f} a {p.mayor_usd:.2f})",
        "  actividad         "
        + (f"{p.por_dia:.1f} op/día sobre {p.dias:.1f} días" if p.por_dia is not None
           else f"ventana de solo {p.dias:.2f} días: demasiado corta para medir el ritmo"),
        f"  comisiones ajenas {p.fee_payer_ratio:.0%}   polvo {p.polvo_ratio:.0%}",
    ]
    if sirve:
        lineas.append("  --> CANDIDATO: pasa la criba. El backtest dirá si copiarlo gana dinero.")
    else:
        lineas.append("  --> DESCARTADA:")
        lineas.extend(f"      - {m}" for m in motivos)
    return "\n".join(lineas)
