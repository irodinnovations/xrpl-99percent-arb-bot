"""Multi-strategy arbitrage scanner: CLOB spreads, AMM pricing, cross-issuer.

Scanning strategies (checked in order for each scan cycle):

1. Same-issuer combined:  For each IOU, get the best buy rate (min of CLOB
   ask, AMM ask) and best sell rate (max of CLOB bid, AMM bid).  If the
   combined best-sell > best-buy + fees → opportunity.  This catches
   cross-venue arb (e.g., buy on CLOB, sell through AMM) automatically.

2. Cross-issuer:  For currencies with multiple issuers (e.g., USD via
   Bitstamp vs GateHub), buy from the cheapest issuer and sell to the
   most expensive.  Path: XRP → IOU(cheap) → IOU(expensive) → XRP.

Rate sources:
  - CLOB: book_offers (order book, no balance dependency)
  - AMM:  amm_info (constant-product pool reserves + trading fee)

The actual execution remains a single atomic Payment transaction using
ALL available liquidity.  The simulation gate validates the real price
before any trade.
"""

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from xrpl.models.requests import BookOffers, AccountLines, AMMInfo

from src.config import PROFIT_THRESHOLD, POSITION_TIERS
from src.connection import XRPLConnection
from src.profit_math import calculate_profit, is_profitable

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")

# Cache trust lines for 5 minutes — they rarely change.
_TRUST_LINE_CACHE_TTL = 300

# Number of offers to fetch from each side of the book.
_BOOK_DEPTH = 10

# Minimum XRP value an offer must have to be used for rate calculation.
# Filters out dust/fake offers placed at absurd prices with negligible
# liquidity (e.g., 0.0001 CNY offered at 500,000 XRP/CNY).
_MIN_OFFER_XRP = Decimal("0.1")


@dataclass
class Opportunity:
    """A single arbitrage opportunity found by the pathfinder."""
    input_xrp: Decimal
    output_xrp: Decimal
    profit_pct: Decimal  # As a percentage (e.g., 0.8 means 0.8%)
    profit_ratio: Decimal  # As a ratio (e.g., 0.008)
    paths: list = field(default_factory=list)
    source_currency: str = "XRP"


@dataclass
class IouRates:
    """Aggregated buy/sell rates for a single IOU across venues."""
    currency: str
    issuer: str
    clob_buy: Optional[Decimal] = None   # CLOB ask (XRP per IOU)
    clob_sell: Optional[Decimal] = None  # CLOB bid (XRP per IOU)
    amm_buy: Optional[Decimal] = None    # AMM ask (XRP per IOU)
    amm_sell: Optional[Decimal] = None   # AMM bid (XRP per IOU)

    @property
    def best_buy(self) -> Optional[Decimal]:
        """Cheapest buy rate across venues (lowest ask wins)."""
        rates = [r for r in [self.clob_buy, self.amm_buy] if r is not None]
        return min(rates) if rates else None

    @property
    def best_sell(self) -> Optional[Decimal]:
        """Best sell rate across venues (highest bid wins)."""
        rates = [r for r in [self.clob_sell, self.amm_sell] if r is not None]
        return max(rates) if rates else None


class PathFinder:
    """Multi-strategy arbitrage scanner."""

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
    # CLOB rate discovery (book_offers)
    # ------------------------------------------------------------------

    async def _get_buy_rate(
        self, currency: str, issuer: str
    ) -> Optional[Decimal]:
        """CLOB ask: XRP cost per unit of IOU (lower = cheaper to buy).

        Scans up to _BOOK_DEPTH offers and returns the rate of the first
        offer whose XRP side meets the _MIN_OFFER_XRP liquidity threshold.
        This filters out dust offers placed at extreme prices.
        """
        request = BookOffers(
            taker_gets={"currency": currency, "issuer": issuer},
            taker_pays={"currency": "XRP"},
            limit=_BOOK_DEPTH,
        )
        result = await self.connection.send_request(request)
        if not result or not result.get("offers"):
            return None

        for offer in result["offers"]:
            try:
                gets = offer.get("TakerGets")
                pays = offer.get("TakerPays")
                if not isinstance(gets, dict) or isinstance(pays, dict):
                    continue
                iou_amount = Decimal(str(gets.get("value", "0")))
                xrp_amount = Decimal(str(pays)) / DROPS_PER_XRP
                if iou_amount <= Decimal("0") or xrp_amount <= Decimal("0"):
                    continue
                if xrp_amount < _MIN_OFFER_XRP:
                    continue
                return xrp_amount / iou_amount
            except (InvalidOperation, ArithmeticError, TypeError) as e:
                logger.debug(f"Buy book parse error: {e}")
                continue
        return None

    async def _get_sell_rate(
        self, currency: str, issuer: str
    ) -> Optional[Decimal]:
        """CLOB bid: XRP received per unit of IOU (higher = better to sell).

        Scans up to _BOOK_DEPTH offers and returns the rate of the first
        offer whose XRP side meets the _MIN_OFFER_XRP liquidity threshold.
        This filters out dust offers placed at extreme prices.
        """
        request = BookOffers(
            taker_gets={"currency": "XRP"},
            taker_pays={"currency": currency, "issuer": issuer},
            limit=_BOOK_DEPTH,
        )
        result = await self.connection.send_request(request)
        if not result or not result.get("offers"):
            return None

        for offer in result["offers"]:
            try:
                gets = offer.get("TakerGets")
                pays = offer.get("TakerPays")
                if isinstance(gets, dict) or not isinstance(pays, dict):
                    continue
                xrp_amount = Decimal(str(gets)) / DROPS_PER_XRP
                iou_amount = Decimal(str(pays.get("value", "0")))
                if iou_amount <= Decimal("0") or xrp_amount <= Decimal("0"):
                    continue
                if xrp_amount < _MIN_OFFER_XRP:
                    continue
                return xrp_amount / iou_amount
            except (InvalidOperation, ArithmeticError, TypeError) as e:
                logger.debug(f"Sell book parse error: {e}")
                continue
        return None

    # ------------------------------------------------------------------
    # AMM rate discovery (amm_info)
    # ------------------------------------------------------------------

    async def _get_amm_rates(
        self, currency: str, issuer: str
    ) -> Optional[tuple[Decimal, Decimal]]:
        """Get AMM buy/sell rates from pool reserves and trading fee.

        AMM uses constant-product formula: x * y = k.
        Mid-price = xrp_reserve / iou_reserve (XRP per IOU).
        The trading fee shifts the effective rate for each direction:
          buy rate (ask)  = mid / (1 - fee)   — you pay more
          sell rate (bid) = mid * (1 - fee)   — you receive less

        Returns (buy_rate, sell_rate) or None if no AMM pool exists.
        """
        request = AMMInfo(
            asset={"currency": "XRP"},
            asset2={"currency": currency, "issuer": issuer},
        )
        result = await self.connection.send_request(request)
        if not result or "amm" not in result:
            return None

        try:
            amm = result["amm"]
            amount = amm.get("amount")
            amount2 = amm.get("amount2")
            fee_bps = Decimal(str(amm.get("trading_fee", 0)))
            # trading_fee is in units of 1/100,000 (e.g., 500 = 0.5%)
            fee_ratio = fee_bps / Decimal("100000")

            # Determine which side is XRP, which is IOU
            if isinstance(amount, str):
                # amount is XRP drops, amount2 is IOU dict
                xrp_reserve = Decimal(str(amount)) / DROPS_PER_XRP
                if not isinstance(amount2, dict):
                    return None
                iou_reserve = Decimal(str(amount2.get("value", "0")))
            elif isinstance(amount2, str):
                # amount2 is XRP drops, amount is IOU dict
                xrp_reserve = Decimal(str(amount2)) / DROPS_PER_XRP
                if not isinstance(amount, dict):
                    return None
                iou_reserve = Decimal(str(amount.get("value", "0")))
            else:
                return None  # Neither side is XRP

            if xrp_reserve <= Decimal("0") or iou_reserve <= Decimal("0"):
                return None

            mid_price = xrp_reserve / iou_reserve  # XRP per IOU
            fee_factor = Decimal("1") - fee_ratio

            if fee_factor <= Decimal("0"):
                return None

            buy_rate = mid_price / fee_factor   # Higher — you pay more to buy
            sell_rate = mid_price * fee_factor  # Lower — you receive less selling

            return (buy_rate, sell_rate)

        except (InvalidOperation, ArithmeticError, TypeError, KeyError) as e:
            logger.debug(f"AMM parse error for {currency}/{issuer[:8]}: {e}")
            return None

    # ------------------------------------------------------------------
    # Rate collection
    # ------------------------------------------------------------------

    async def _collect_rates(self, trust_lines: list[dict]) -> list[IouRates]:
        """Fetch CLOB + AMM rates for all trust-lined IOUs.

        3 RPC calls per IOU: buy book, sell book, amm_info.
        Total: 81 calls for 27 IOUs.
        """
        all_rates: list[IouRates] = []

        for line in trust_lines:
            currency = line["currency"]
            issuer = line["account"]
            rates = IouRates(currency=currency, issuer=issuer)

            rates.clob_buy = await self._get_buy_rate(currency, issuer)
            rates.clob_sell = await self._get_sell_rate(currency, issuer)

            amm = await self._get_amm_rates(currency, issuer)
            if amm:
                rates.amm_buy, rates.amm_sell = amm

            all_rates.append(rates)

        return all_rates

    # ------------------------------------------------------------------
    # Path construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_path(currency: str, issuer: str) -> list:
        """Single-hop path: XRP -> IOU -> XRP through one issuer."""
        return [[{
            "currency": currency,
            "issuer": issuer,
            "type": 48,
            "type_hex": "0000000000000030",
        }]]

    @staticmethod
    def _build_cross_issuer_path(
        currency: str, buy_issuer: str, sell_issuer: str
    ) -> list:
        """Two-hop path: XRP -> IOU(buy_issuer) -> IOU(sell_issuer) -> XRP.

        The payment engine handles the cross-issuer transfer at step 2
        via rippling or existing cross-issuer offers.
        """
        return [[
            {
                "currency": currency,
                "issuer": buy_issuer,
                "type": 48,
                "type_hex": "0000000000000030",
            },
            {
                "currency": currency,
                "issuer": sell_issuer,
                "type": 48,
                "type_hex": "0000000000000030",
            },
        ]]

    # ------------------------------------------------------------------
    # Spread checks
    # ------------------------------------------------------------------

    def _check_spread(
        self,
        currency: str,
        issuer: str,
        buy_rate: Decimal,
        sell_rate: Decimal,
        position_xrp: Decimal,
        volatility_factor: Decimal,
        paths: Optional[list] = None,
        label: str = "",
    ) -> Optional[Opportunity]:
        """Check if a buy/sell rate pair yields a profitable round-trip.

        output_xrp = position_xrp * (sell_rate / buy_rate)
        Profit if output > input + fees + slippage.
        """
        if sell_rate <= buy_rate:
            return None

        output_xrp = position_xrp * sell_rate / buy_rate

        if output_xrp <= position_xrp:
            return None

        profit_ratio = calculate_profit(position_xrp, output_xrp, volatility_factor)
        if not is_profitable(position_xrp, output_xrp, volatility_factor):
            return None

        profit_pct = profit_ratio * Decimal("100")
        tag = f" [{label}]" if label else ""
        logger.info(
            f"OPPORTUNITY{tag}: {currency}/{issuer[:8]}... | "
            f"{profit_pct:.4f}% profit | "
            f"in={position_xrp:.6f} -> out={output_xrp:.6f} XRP | "
            f"ask={buy_rate:.8f} bid={sell_rate:.8f}"
        )

        return Opportunity(
            input_xrp=position_xrp,
            output_xrp=output_xrp,
            profit_pct=profit_pct,
            profit_ratio=profit_ratio,
            paths=paths if paths is not None else self._build_path(currency, issuer),
            source_currency="XRP",
        )

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    async def scan(
        self,
        account_balance: Decimal,
        volatility_factor: Decimal = Decimal("0"),
        position_tiers: Optional[list[Decimal]] = None,
    ) -> list[Opportunity]:
        """Multi-strategy scan: CLOB spreads, AMM pricing, cross-issuer.

        Phase 1: Collect CLOB + AMM rates for all 27 IOUs (81 RPC calls).
        Phase 2: Same-issuer — best buy vs best sell across venues.
        Phase 3: Cross-issuer — cheapest buy vs best sell across issuers.

        Rate discovery is per-IOU (not per-tier).  Tier checks are pure math.

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

        # Phase 1: Collect all rates
        all_rates = await self._collect_rates(trust_lines)

        all_opps: list[Opportunity] = []
        rated_count = 0
        amm_count = sum(1 for r in all_rates if r.amm_buy is not None)

        # Phase 2: Same-issuer combined spreads
        for rates in all_rates:
            best_buy = rates.best_buy
            best_sell = rates.best_sell
            if best_buy is None or best_sell is None:
                continue
            rated_count += 1

            for tier in tiers:
                position_xrp = account_balance * tier
                if position_xrp <= Decimal("0"):
                    continue
                opp = self._check_spread(
                    rates.currency, rates.issuer,
                    best_buy, best_sell,
                    position_xrp, volatility_factor,
                    label="same-issuer",
                )
                if opp:
                    all_opps.append(opp)

        # Phase 3: Cross-issuer arbitrage
        groups: dict[str, list[IouRates]] = defaultdict(list)
        for rates in all_rates:
            if rates.best_buy is not None or rates.best_sell is not None:
                groups[rates.currency].append(rates)

        for currency, issuers in groups.items():
            if len(issuers) < 2:
                continue

            # Find cheapest buy across all issuers of this currency
            buy_candidates = [
                r for r in issuers if r.best_buy is not None
            ]
            sell_candidates = [
                r for r in issuers if r.best_sell is not None
            ]
            if not buy_candidates or not sell_candidates:
                continue

            cheapest = min(buy_candidates, key=lambda r: r.best_buy)
            richest = max(sell_candidates, key=lambda r: r.best_sell)

            # Skip if same issuer — already checked in phase 2
            if cheapest.issuer == richest.issuer:
                continue

            cross_path = self._build_cross_issuer_path(
                currency, cheapest.issuer, richest.issuer,
            )

            for tier in tiers:
                position_xrp = account_balance * tier
                if position_xrp <= Decimal("0"):
                    continue
                opp = self._check_spread(
                    currency, cheapest.issuer,
                    cheapest.best_buy, richest.best_sell,
                    position_xrp, volatility_factor,
                    paths=cross_path,
                    label=f"cross-issuer {cheapest.issuer[:8]}→{richest.issuer[:8]}",
                )
                if opp:
                    all_opps.append(opp)

        logger.debug(
            f"Scan complete: {len(all_rates)} IOUs, {rated_count} with books, "
            f"{amm_count} with AMM, {len(all_opps)} opportunities"
        )
        return _deduplicate_opportunities(all_opps)


# ------------------------------------------------------------------
# Deduplication helpers
# ------------------------------------------------------------------

def _path_signature(paths: list) -> str:
    """Create a hashable signature from a paths list for deduplication."""
    return json.dumps(paths, sort_keys=True, default=str)


def _deduplicate_opportunities(opportunities: list[Opportunity]) -> list[Opportunity]:
    """Deduplicate by path, keeping highest profit ratio per path."""
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
