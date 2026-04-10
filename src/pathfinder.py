"""Pathfinder using ripple_path_find for hybrid AMM+CLOB arbitrage discovery."""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from xrpl.models.requests import RipplePathFind

from src.config import PROFIT_THRESHOLD
from src.connection import XRPLConnection
from src.profit_math import calculate_profit, is_profitable, calculate_position_size

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")


@dataclass
class Opportunity:
    """A single arbitrage opportunity found by the pathfinder."""
    input_xrp: Decimal
    output_xrp: Decimal
    profit_pct: Decimal  # As a percentage (e.g., 0.8 means 0.8%)
    profit_ratio: Decimal  # As a ratio (e.g., 0.008)
    paths: list = field(default_factory=list)
    source_currency: str = "XRP"


class PathFinder:
    """Scans for arbitrage opportunities via ripple_path_find."""

    def __init__(self, connection: XRPLConnection, wallet_address: str):
        self.connection = connection
        self.wallet_address = wallet_address

    def build_path_request(self, input_xrp: Decimal) -> RipplePathFind:
        """Build a ripple_path_find request for XRP-to-XRP loop.

        We send XRP, route through intermediate tokens via AMM+CLOB,
        and receive XRP back. If output > input after fees, it's arbitrage.
        """
        destination_drops = str(int(input_xrp * DROPS_PER_XRP))
        return RipplePathFind(
            source_account=self.wallet_address,
            destination_account=self.wallet_address,
            destination_amount=destination_drops,
            source_currencies=[{"currency": "XRP"}],
        )

    def parse_alternatives(
        self,
        response: Optional[dict],
        input_xrp: Decimal,
        volatility_factor: Decimal = Decimal("0"),
    ) -> list[Opportunity]:
        """Parse ripple_path_find response into profitable Opportunity objects.

        Only returns opportunities that exceed PROFIT_THRESHOLD.
        Amounts from XRPL node are parsed through Decimal(str(...)) to prevent
        float contamination and handle malformed values gracefully (T-01-05).
        """
        if not response or "alternatives" not in response:
            return []

        opportunities = []
        for alt in response["alternatives"]:
            try:
                # source_amount is in drops (string) for XRP
                source_amount_raw = alt.get("source_amount", "0")
                if isinstance(source_amount_raw, dict):
                    # Non-XRP source — skip for now (XRP-only strategy)
                    continue
                source_drops = Decimal(str(source_amount_raw))
                source_xrp = source_drops / DROPS_PER_XRP

                # output is the destination_amount we requested
                output_xrp = input_xrp

                # For arbitrage: we pay source_xrp and receive input_xrp back
                # Profit = what we get back minus what we pay
                if source_xrp >= output_xrp:
                    continue  # No profit if we pay more than we receive

                profit_ratio = calculate_profit(source_xrp, output_xrp, volatility_factor)

                if not is_profitable(source_xrp, output_xrp, volatility_factor):
                    continue

                profit_pct = profit_ratio * Decimal("100")
                paths = alt.get("paths_computed", [])

                opportunities.append(Opportunity(
                    input_xrp=source_xrp,
                    output_xrp=output_xrp,
                    profit_pct=profit_pct,
                    profit_ratio=profit_ratio,
                    paths=paths,
                    source_currency="XRP",
                ))
                logger.info(
                    f"Opportunity found: {profit_pct:.4f}% profit "
                    f"(in={source_xrp} XRP, out={output_xrp} XRP)"
                )

            except (ValueError, KeyError, ArithmeticError) as e:
                logger.warning(f"Failed to parse alternative: {e}")
                continue

        return opportunities

    async def scan(
        self,
        account_balance: Decimal,
        volatility_factor: Decimal = Decimal("0"),
    ) -> list[Opportunity]:
        """Run a single scan cycle: compute position size, path_find, parse results."""
        position_size = calculate_position_size(account_balance)
        if position_size <= Decimal("0"):
            logger.warning("Position size is zero — skipping scan")
            return []

        request = self.build_path_request(position_size)
        response = await self.connection.send_request(request)
        return self.parse_alternatives(response, position_size, volatility_factor)
