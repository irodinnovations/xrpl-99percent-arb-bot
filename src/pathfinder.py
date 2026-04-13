"""Two-leg arbitrage scanner using book_offers for rate discovery.

Strategy: For each trust-lined IOU, query both sides of the order book:
  Buy side  (ask): book_offers where taker gets IOU, pays XRP
  Sell side (bid): book_offers where taker gets XRP, pays IOU
  If best bid > best ask + fees + slippage -> arbitrage opportunity.

book_offers returns the CLOB order book directly and does NOT require
the account to hold the source currency (unlike ripple_path_find, which
returns empty alternatives when the wallet has 0 IOU balance).

The actual execution remains a single atomic Payment transaction
(XRP -> IOU -> XRP) which uses ALL available liquidity (AMM + CLOB).
The simulation gate validates the real execution price before any trade.

Why book_offers instead of ripple_path_find:
  ripple_path_find for the sell leg (IOU -> XRP) requires the source
  account to hold the IOU.  Since the bot never accumulates IOUs
  (round-trip trades are atomic), the wallet always has 0 IOU balance,
  causing all sell probes to return "no path".  book_offers has no such
  restriction — it shows available offers regardless of the caller's
  balance.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from xrpl.models.requests import BookOffers, AccountLines

from src.config import PROFIT_THRESHOLD, POSITION_TIERS
from src.connection import XRPLConnection
from src.profit_math import calculate_profit, is_profitable

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")

# Cache trust lines for 5 minutes — they rarely change.
_TRUST_LINE_CACHE_TTL = 300

# Number of offers to fetch from each side of the book.
# More offers = better depth estimate, but diminishing returns.
_BOOK_DEPTH = 10


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
    """Two-leg arbitrage scanner using order book rate discovery."""

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
    # Order book rate discovery
    # ------------------------------------------------------------------

    async def _get_buy_rate(
        self, currency: str, issuer: str
    ) -> Optional[Decimal]:
        """Get the effective rate to BUY this IOU with XRP (ask price).

        Queries the order book where taker gets IOU and pays XRP.
        Returns XRP cost per unit of IOU (lower is better for buying),
        or None if the book is empty.

        Uses the best (top) offer for rate discovery.  The simulation
        gate validates the actual execution price at trade time.
        """
        request = BookOffers(
            taker_gets={"currency": currency, "issuer": issuer},
            taker_pays={"currency": "XRP"},
            limit=_BOOK_DEPTH,
        )
        result = await self.connection.send_request(request)
        if not result or not result.get("offers"):
            return None

        offer = result["offers"][0]
        try:
            # TakerGets = IOU amount (dict), TakerPays = XRP drops (string)
            gets = offer.get("TakerGets")
            pays = offer.get("TakerPays")

            if not isinstance(gets, dict) or isinstance(pays, dict):
                logger.debug(f"Buy book unexpected format: gets={gets}, pays={pays}")
                return None

            iou_amount = Decimal(str(gets.get("value", "0")))
            xrp_amount = Decimal(str(pays)) / DROPS_PER_XRP

            if iou_amount <= Decimal("0") or xrp_amount <= Decimal("0"):
                return None

            return xrp_amount / iou_amount  # XRP per IOU

        except (InvalidOperation, ArithmeticError, TypeError) as e:
            logger.debug(f"Buy book parse error: {e}")
            return None

    async def _get_sell_rate(
        self, currency: str, issuer: str
    ) -> Optional[Decimal]:
        """Get the effective rate to SELL this IOU for XRP (bid price).

        Queries the order book where taker gets XRP and pays IOU.
        Returns XRP received per unit of IOU (higher is better for selling),
        or None if the book is empty.
        """
        request = BookOffers(
            taker_gets={"currency": "XRP"},
            taker_pays={"currency": currency, "issuer": issuer},
            limit=_BOOK_DEPTH,
        )
        result = await self.connection.send_request(request)
        if not result or not result.get("offers"):
            return None

        offer = result["offers"][0]
        try:
            # TakerGets = XRP drops (string), TakerPays = IOU amount (dict)
            gets = offer.get("TakerGets")
            pays = offer.get("TakerPays")

            if isinstance(gets, dict) or not isinstance(pays, dict):
                logger.debug(f"Sell book unexpected format: gets={gets}, pays={pays}")
                return None

            xrp_amount = Decimal(str(gets)) / DROPS_PER_XRP
            iou_amount = Decimal(str(pays.get("value", "0")))

            if iou_amount <= Decimal("0") or xrp_amount <= Decimal("0"):
                return None

            return xrp_amount / iou_amount  # XRP per IOU

        except (InvalidOperation, ArithmeticError, TypeError) as e:
            logger.debug(f"Sell book parse error: {e}")
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

    def _check_spread(
        self,
        currency: str,
        issuer: str,
        buy_rate: Decimal,
        sell_rate: Decimal,
        position_xrp: Decimal,
        volatility_factor: Decimal,
    ) -> Optional[Opportunity]:
        """Check if the bid/ask spread yields a profitable round-trip.

        Round-trip: spend position_xrp -> buy IOU at ask -> sell at bid.
          iou_bought = position_xrp / buy_rate
          output_xrp = iou_bought * sell_rate
                     = position_xrp * (sell_rate / buy_rate)
        Profit if output_xrp > position_xrp + fees + slippage.
        """
        if sell_rate <= buy_rate:
            return None  # No spread — bid <= ask

        output_xrp = position_xrp * sell_rate / buy_rate

        if output_xrp <= position_xrp:
            return None

        profit_ratio = calculate_profit(position_xrp, output_xrp, volatility_factor)
        if not is_profitable(position_xrp, output_xrp, volatility_factor):
            return None

        profit_pct = profit_ratio * Decimal("100")
        logger.info(
            f"OPPORTUNITY: {currency}/{issuer[:8]}... | "
            f"{profit_pct:.4f}% profit | "
            f"in={position_xrp:.6f} XRP -> out={output_xrp:.6f} XRP | "
            f"ask={buy_rate:.8f} bid={sell_rate:.8f}"
        )

        return Opportunity(
            input_xrp=position_xrp,
            output_xrp=output_xrp,
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
        """Scan all trust-lined IOUs for order-book arbitrage.

        For each IOU:
          1. Fetch buy rate (ask) and sell rate (bid) from book_offers
          2. For each position tier: check if bid > ask + fees + slippage
          3. If profitable -> create Opportunity

        Rate discovery is 2 calls per IOU (54 total for 27 IOUs).
        Tier checks are pure math — no additional RPC calls.
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
        books_found = 0

        for line in trust_lines:
            currency = line["currency"]
            issuer = line["account"]

            # Two RPC calls per IOU: buy book + sell book
            buy_rate = await self._get_buy_rate(currency, issuer)
            sell_rate = await self._get_sell_rate(currency, issuer)
            probed += 1

            if buy_rate is None or sell_rate is None:
                continue

            books_found += 1

            # Check each position tier (pure math, no RPC)
            for tier in tiers:
                position_xrp = account_balance * tier
                if position_xrp <= Decimal("0"):
                    continue

                opp = self._check_spread(
                    currency, issuer,
                    buy_rate, sell_rate,
                    position_xrp, volatility_factor,
                )
                if opp:
                    all_opps.append(opp)

        logger.debug(
            f"Scan complete: {probed} IOUs probed, {books_found} with books, "
            f"{len(all_opps)} opportunities"
        )
        return _deduplicate_opportunities(all_opps)


# ------------------------------------------------------------------
# Deduplication helpers
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
