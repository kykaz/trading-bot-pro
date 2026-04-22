from __future__ import annotations

from trading_bot.config import PolymarketConfig


class PolymarketClient:
    """
    Punto de integración para el cliente oficial del exchange.

    Mantengo este archivo como adaptador para que la estrategia, el riesgo y la
    ejecución no dependan directamente del SDK externo. Así luego podemos
    cambiar mock -> real sin rediseñar el resto del bot.
    """

    def __init__(self, config: PolymarketConfig) -> None:
        self.config = config

    def validate_live_mode(self) -> None:
        if not self.config.enable_live_trading:
            raise RuntimeError(
                "Live trading is disabled. Keep paper mode enabled until the strategy is validated."
            )
