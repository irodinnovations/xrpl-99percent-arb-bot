"""Pathfinder using ripple_path_find for hybrid AMM+CLOB arbitrage discovery."""

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from xrpl.models.requests import RipplePathFind

from src.config import PROFIT_THRESHOLD, POSITION_TIERS
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
        position_tiers: Optional[list[Decimal]] = None,
    ) -> list[Opportunity]:
        """Run a multi-tier scan cycle: probe at multiple position sizes per ledger.

        Different trade amounts surface different paths — a small probe may find
        thin AMM pools, while a larger probe finds CLOB depth. Each tier calls
        ripple_path_find independently (free RPC calls) and results are merged
        with deduplication (highest profit ratio kept per unique path).

        Args:
            account_balance: Current XRP balance.
            volatility_factor: 0-1 volatility estimate for slippage calculation.
            position_tiers: List of Decimal fractions (e.g., [0.01, 0.05, 0.10]).
                            Defaults to POSITION_TIERS from config.
        """
        tiers = position_tiers if position_tiers is not None else POSITION_TIERS
        all_opportunities: list[Opportunity] = []

        for tier in tiers:
            position_size = account_balance * tier
            if position_size <= Decimal("0"):
                continue

            request = self.build_path_request(position_size)
            response = await self.connection.send_request(request)
            opps = self.parse_alternatives(response, position_size, volatility_factor)

            if opps:
                logger.info(
                    f"Tier {tier * 100:.0f}%: {len(opps)} opportunity(s) "
                    f"at {position_size:.2f} XRP"
                )
            all_opportunities.extend(opps)

        return _deduplicate_opportunities(all_opportunities)


def _path_signature(paths: list) -> str:
    """Create a hashable signature from a paths list for deduplication.

    Two opportunities with identical path routes (same intermediate hops)
    are considered duplicates — we keep whichever has the best profit ratio.
    """
    return json.dumps(paths, sort_keys=True, default=str)


def _deduplicate_opportunities(opportunities: list[Opportunity]) -> list[Opportunity]:
    """Deduplicate opportunities by path, keeping highest profit ratio per path.

    When multiple tiers discover the same route, the tier that yields the best
    net profit ratio wins. This prevents the executor from submitting the same
    path twice at different amounts.
    """
    if len(opportunities) <= 1:
        return opportunities

    best_by_path: dict[str, Opportunity] = {}
    for opp in opportunities:
        sig = _path_signature(opp.paths)
        existing = best_by_path.get(sig)
        if existing is None or opp.profit_ratio > existing.profit_ratio:
            best_by_path[sig] = opp

    deduped = list(best_by_path.values())
    if len(deduped) < len(opportunities):
        logger.info(
            f"Deduplicated: {len(opportunities)} -> {len(deduped)} unique paths"
        )
    return deduped
