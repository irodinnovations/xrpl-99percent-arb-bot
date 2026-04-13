"""Two-leg arbitrage scanner using ripple_path_find.

Strategy: For each trust-lined IOU, probe two legs:
  Leg 1 (sell probe): How much IOU to receive target XRP?
  Leg 2 (buy probe):  How much XRP to acquire that IOU?
  If buy cost < target XRP -> the round-trip is profitable.

ripple_path_find considers both AMM pools and CLOB order books, so this
catches opportunities from either venue.  The actual execution remains a
single atomic Payment transaction (XRP -> IOU -> XRP) handled by the
existing executor — only the *discovery* strategy changes.

Why two legs instead of one:
  ripple_path_find does NOT discover circular XRP->IOU->XRP routes when
  source_account = destination_account and both amounts are XRP.  It
  returns an empty alternatives array every time.  However, the XRP->IOU
  direction works fine, as does IOU->XRP.  By splitting into two probes
  we get the effective buy/sell rates and can detect arbitrage.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from xrpl.models.requests import RipplePathFind, AccountLines

from src.config import PROFIT_THRESHOLD, POSITION_TIERS
from src.connection import XRPLConnection
from src.profit_math import calculate_profit, is_profitable

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")

# Cache trust lines for 5 minutes — they rarely change.
_TRUST_LINE_CACHE_TTL = 300


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
    """Two-leg arbitrage scanner across trust-lined IOUs."""

    def __init__(self, connection: XRPLConnection, wallet_address: str):
        self.connection = connection
        self.wallet_address = wallet_address
        self._trust_lines: list[dict] = []
        self._trust_lines_ts: float = 0

    # ------------------------------------------------------------------
    # Trust line discovery
    # ------------------------------------------------------------------

    async def _fetch_trust_lines(self) -> list[dict]:
        """Fetch account trust lines via account_lines, cached for 5 min."""
        now = time.time()
        if self._trust_lines and (now - self._trust_lines_ts) < _TRUST_LINE_CACHE_TTL:
            return self._trust_lines

        request = AccountLines(account=self.wallet_address)
        result = await self.connection.send_request(request)
        if not result or "lines" not in result:
            logger.warning("Failed to fetch trust lines — using stale cache")
            return self._trust_lines

        self._trust_lines = result["lines"]
        self._trust_lines_ts = now
        logger.info(f"Fetched {len(self._trust_lines)} trust lines")
        return self._trust_lines

    # ------------------------------------------------------------------
    # ripple_path_find helpers
    # ------------------------------------------------------------------

    async def _path_find(
        self,
        dest_amount,
        source_currencies: list[dict],
    ) -> Optional[dict]:
        """Send ripple_path_find and return the best (first) alternative.

        Returns the raw alternative dict or None if no paths found.
        """
        request = RipplePathFind(
            source_account=self.wallet_address,
            destination_account=self.wallet_address,
            destination_amount=dest_amount,
            source_currencies=source_currencies,
        )
        result = await self.connection.send_request(request)
        if not result:
            return None
        alts = result.get("alternatives", [])
        return alts[0] if alts else None

    async def _probe_buy_cost(
        self, currency: str, issuer: str, iou_amount: Decimal
    ) -> Optional[Decimal]:
        """How much XRP to buy *iou_amount* of the specified IOU?

        Calls ripple_path_find with:
          destination_amount = {currency, issuer, value}
          source_currencies  = [XRP]

        Returns the XRP cost (Decimal) or None if no path found.
        """
        dest = {
            "currency": currency,
            "issuer": issuer,
            "value": str(iou_amount),
        }
        alt = await self._path_find(dest, [{"currency": "XRP"}])
        if alt is None:
            return None

        source = alt.get("source_amount", "0")
        # XRP source_amount is a string of drops, not a dict
        if isinstance(source, dict):
            logger.debug(f"Buy probe got IOU source (expected drops): {source}")
            return None
        try:
            return Decimal(str(source)) / DROPS_PER_XRP
        except (InvalidOperation, ArithmeticError) as e:
            logger.debug(f"Buy probe parse error: {e}")
            return None

    async def _probe_sell_cost(
        self, currency: str, issuer: str, xrp_amount: Decimal
    ) -> Optional[Decimal]:
        """How much IOU is needed to receive *xrp_amount* XRP?

        Calls ripple_path_find with:
          destination_amount = drops string (XRP)
          source_currencies  = [{currency, issuer}]

        Returns the IOU amount needed (Decimal) or None if no path found.
        """
        dest_drops = str(int(xrp_amount * DROPS_PER_XRP))
        alt = await self._path_find(
            dest_drops,
            [{"currency": currency, "issuer": issuer}],
        )
        if alt is None:
            return None

        source = alt.get("source_amount")
        # IOU source_amount is a dict {currency, issuer, value}
        if not isinstance(source, dict):
            logger.debug(f"Sell probe got drops (expected IOU dict): {source}")
            return None
        try:
            return Decimal(str(source.get("value", "0")))
        except (InvalidOperation, ArithmeticError) as e:
            logger.debug(f"Sell probe parse error: {e}")
            return None

    # ------------------------------------------------------------------
    # Path construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_path(currency: str, issuer: str) -> list:
        """Construct XRPL Paths for routing through a single intermediate IOU.

        Format: [[{currency, issuer, type: 48}]]

        The payment engine converts XRP -> IOU at the first hop (using the
        best available AMM/CLOB offers) and IOU -> XRP at the second hop.
        type 48 = 0x30 = currency (0x10) + issuer (0x20).
        """
        return [[{
            "currency": currency,
            "issuer": issuer,
            "type": 48,
            "type_hex": "0000000000000030",
        }]]

    # ------------------------------------------------------------------
    # Single-IOU round-trip check
    # ------------------------------------------------------------------

    async def _check_iou(
        self,
        currency: str,
        issuer: str,
        position_xrp: Decimal,
        volatility_factor: Decimal,
    ) -> Optional[Opportunity]:
        """Two-leg round-trip arbitrage check for a single IOU.

        1. Sell probe: how much IOU to receive position_xrp back?
        2. Buy probe: how much XRP to acquire that much IOU?
        3. If buy_cost < position_xrp -> round-trip is profitable.

        The actual execution path (XRP -> IOU -> XRP) is a single atomic
        Payment transaction — the XRPL engine handles the intermediate
        swaps without the user needing to hold the IOU beforehand.
        """
        # Leg 1 — sell probe: IOU cost to get position_xrp XRP
        sell_iou = await self._probe_sell_cost(currency, issuer, position_xrp)
        if sell_iou is None or sell_iou <= Decimal("0"):
            return None

        # Leg 2 — buy probe: XRP cost to acquire sell_iou units of this IOU
        buy_xrp = await self._probe_buy_cost(currency, issuer, sell_iou)
        if buy_xrp is None or buy_xrp <= Decimal("0"):
            return None

        # Round-trip check: spend buy_xrp -> get sell_iou IOU -> sell for position_xrp
        if buy_xrp >= position_xrp:
            return None  # No profit — cost equals or exceeds return

        # Verify profit exceeds threshold after fees and slippage
        profit_ratio = calculate_profit(buy_xrp, position_xrp, volatility_factor)
        if not is_profitable(buy_xrp, position_xrp, volatility_factor):
            return None

        profit_pct = profit_ratio * Decimal("100")
        logger.info(
            f"OPPORTUNITY: {currency}/{issuer[:8]}... | "
            f"{profit_pct:.4f}% profit | "
            f"buy={buy_xrp:.6f} XRP -> sell={position_xrp:.6f} XRP | "
            f"via {sell_iou} IOU"
        )

        return Opportunity(
            input_xrp=buy_xrp,
            output_xrp=position_xrp,
            profit_pct=profit_pct,
            profit_ratio=profit_ratio,
            paths=self._build_path(currency, issuer),
            source_currency="XRP",
        )

    # ------------------------------------------------------------------
    # Main scan entry point
    # ------------------------------------------------------------------

    async def scan(
        self,
        account_balance: Decimal,
        volatility_factor: Decimal = Decimal("0"),
        position_tiers: Optional[list[Decimal]] = None,
    ) -> list[Opportunity]:
        """Multi-tier two-leg scan across all trust-lined IOUs.

        For each IOU x tier:
          1. Sell probe: how much IOU to get target XRP?
          2. Buy probe: how much XRP to acquire that IOU?
          3. If buy < sell target -> arbitrage opportunity.

        With 27 trust lines, 3 tiers, 2 probes each = 162 path_find calls.
        Call this every N ledgers (SCAN_INTERVAL), not every ledger.

        Args:
            account_balance: Current XRP balance.
            volatility_factor: 0-1 Decimal for slippage calculation.
            position_tiers: Fraction of balance per tier (default from config).
        """
        tiers = position_tiers if position_tiers is not None else POSITION_TIERS
        trust_lines = await self._fetch_trust_lines()

        if not trust_lines:
            logger.warning("No trust lines found — nothing to scan")
            return []

        all_opps: list[Opportunity] = []
        probed = 0

        for line in trust_lines:
            currency = line["currency"]
            issuer = line["account"]

            for tier in tiers:
                position_xrp = account_balance * tier
                if position_xrp <= Decimal("0"):
                    continue

                opp = await self._check_iou(
                    currency, issuer, position_xrp, volatility_factor,
                )
                probed += 1
                if opp:
                    all_opps.append(opp)

        logger.debug(f"Scan complete: {probed} probes, {len(all_opps)} opportunities")
        return _deduplicate_opportunities(all_opps)


# ------------------------------------------------------------------
# Deduplication helpers (unchanged from original)
# ------------------------------------------------------------------

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
