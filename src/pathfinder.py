"""Multi-strategy arbitrage scanner: CLOB spreads, AMM pricing, cross-issuer.

Scanning strategies (checked in order for each scan cycle):

1. Same-issuer combined:  For each IOU, get the best buy rate (min of CLOB
   ask, AMM ask) and best sell rate (max of CLOB bid, AMM bid).  If the
   combined best-sell > best-buy + fees -> opportunity.  This catches
   cross-venue arb (e.g., buy on CLOB, sell through AMM) automatically.

2. Cross-issuer:  For currencies with multiple issuers (e.g., USD via
   Bitstamp vs GateHub), buy from the cheapest issuer and sell to the
   most expensive.  Path: XRP -> IOU(cheap) -> IOU(expensive) -> XRP.

3. Multi-hop discovery:  Uses ripple_path_find with source=destination to
   discover circular arb paths through 3+ hops that manual construction
   would miss.  The XRPL server's pathfinding explores the full order book
   + AMM + trust line graph automatically.

Rate sources:
  - CLOB: book_offers (volume-weighted across multiple book levels)
  - AMM:  amm_info (constant-product pool reserves + trading fee)

Performance:
  All 27 IOUs are fetched in parallel via asyncio.gather (3 RPC calls
  per IOU run sequentially within each coroutine, but all 27 coroutines
  run concurrently).  Effective scan time: ~3 round-trips instead of ~81.

  Event-driven mode: scan_pairs() scans only the IOUs whose order books
  changed in the latest ledger (triggered by book_changes stream), achieving
  ~4-7 second reaction time.

The actual execution remains a single atomic Payment transaction using
ALL available liquidity.  The simulation gate validates the real price
before any trade.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from xrpl.models.requests import BookOffers, AccountLines, AMMInfo, RipplePathFind

from src.config import PROFIT_THRESHOLD
from src.connection import XRPLConnection
from src.profit_math import (
    calculate_profit,
    is_profitable,
    get_profit_threshold,
    calculate_dynamic_position,
)

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")

# Cache trust lines for 5 minutes -- they rarely change.
_TRUST_LINE_CACHE_TTL = 300

# Number of offers to fetch from each side of the book.
_BOOK_DEPTH = 10

# Minimum XRP value an offer must have to be used for rate calculation.
# Filters out dust/fake offers placed at absurd prices with negligible
# liquidity (e.g., 0.0001 CNY offered at 500,000 XRP/CNY).
_MIN_OFFER_XRP = Decimal("0.1")

# Maximum plausible profit percentage.  Real XRPL arb is typically 0.6-1%.
# Anything above this is a stale/joke offer on an illiquid book -- not a
# real opportunity.  Reject it before it reaches simulation.
_MAX_PROFIT_PCT = Decimal("5")


@dataclass
class Opportunity:
    """A single arbitrage opportunity — executed as two sequential Payments.

    XRPL forbids atomic XRP->IOU->XRP in one Payment (rippled returns
    temBAD_SEND_XRP_MAX / temBAD_SEND_XRP_PATHS). Every opportunity is
    therefore structured as two legs:

      Leg 1: spend `input_xrp` XRP to acquire `iou_amount` of
             `iou_currency/buy_issuer` (xrpDirect=false, legal)

      Leg 2: spend the held IOU (as SendMax) to receive `output_xrp` XRP
             routed through `sell_issuer`'s book (for cross-issuer arb)
             or directly against `buy_issuer`'s book (for same-issuer).

    The executor uses this shape to build both transactions, simulate
    both before submitting either, and execute them sequentially with
    tight LastLedgerSequence windows.  See docs/two_leg_architecture.md.
    """
    input_xrp: Decimal
    output_xrp: Decimal
    profit_pct: Decimal  # As a percentage (e.g., 0.8 means 0.8%)
    profit_ratio: Decimal  # As a ratio (e.g., 0.008)

    # Two-leg execution fields
    iou_currency: str = ""          # e.g. "USD" or a hex code
    buy_issuer: str = ""            # leg 1 destination issuer (cheap side)
    sell_issuer: str = ""           # leg 2 routing issuer (rich side; == buy for same-issuer)
    iou_amount: Decimal = Decimal("0")  # IOU acquired in leg 1, spent in leg 2

    # Legacy field retained for multi-hop paths that still use the old
    # single-Payment model during the transition window. New code should
    # prefer the two-leg fields above.
    paths: list = field(default_factory=list)
    source_currency: str = "XRP"

    def route_key(self) -> str:
        """Stable key identifying this route — used by the Blacklist."""
        return f"{self.iou_currency}|{self.buy_issuer}|{self.sell_issuer}"

    def is_cross_issuer(self) -> bool:
        return bool(self.buy_issuer) and bool(self.sell_issuer) and self.buy_issuer != self.sell_issuer


@dataclass
class IouRates:
    """Aggregated buy/sell rates for a single IOU across venues."""
    currency: str
    issuer: str
    clob_buy: Optional[Decimal] = None   # CLOB ask (XRP per IOU)
    clob_sell: Optional[Decimal] = None  # CLOB bid (XRP per IOU)
    amm_buy: Optional[Decimal] = None    # AMM ask (XRP per IOU)
    amm_sell: Optional[Decimal] = None   # AMM bid (XRP per IOU)
    clob_buy_depth_xrp: Decimal = Decimal("0")  # Total XRP liquidity on buy side
    clob_sell_depth_xrp: Decimal = Decimal("0")  # Total XRP liquidity on sell side

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
    """Multi-strategy arbitrage scanner with parallel RPC."""

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
            logger.warning("Failed to fetch trust lines -- using stale cache")
            return self._trust_lines

        self._trust_lines = result["lines"]
        self._trust_lines_ts = now
        logger.info(f"Fetched {len(self._trust_lines)} trust lines")
        return self._trust_lines

    # ------------------------------------------------------------------
    # CLOB rate discovery (volume-weighted across book levels)
    # ------------------------------------------------------------------

    async def _get_buy_rate(
        self, currency: str, issuer: str, target_xrp: Optional[Decimal] = None,
    ) -> tuple[Optional[Decimal], Decimal]:
        """CLOB ask: volume-weighted XRP cost per unit of IOU.

        When target_xrp is provided, calculates the effective rate to fill
        that amount across multiple book levels.  Otherwise returns the
        best qualifying offer rate (backward compatible).

        Returns (rate, depth_xrp) where depth_xrp is total qualifying
        liquidity on the book side.
        """
        request = BookOffers(
            taker_gets={"currency": currency, "issuer": issuer},
            taker_pays={"currency": "XRP"},
            limit=_BOOK_DEPTH,
        )
        result = await self.connection.send_request(request)
        if not result or not result.get("offers"):
            return None, Decimal("0")

        qualifying_offers: list[tuple[Decimal, Decimal]] = []  # (xrp_amount, iou_amount)

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
                qualifying_offers.append((xrp_amount, iou_amount))
            except (InvalidOperation, ArithmeticError, TypeError) as e:
                logger.debug(f"Buy book parse error: {e}")
                continue

        if not qualifying_offers:
            return None, Decimal("0")

        total_depth = sum(o[0] for o in qualifying_offers)

        # Volume-weighted rate across book levels for a specific trade size
        if target_xrp is not None and target_xrp > Decimal("0"):
            remaining = target_xrp
            total_iou = Decimal("0")
            for xrp_avail, iou_avail in qualifying_offers:
                rate = xrp_avail / iou_avail
                fill = min(remaining, xrp_avail)
                total_iou += fill / rate
                remaining -= fill
                if remaining <= Decimal("0"):
                    break
            if total_iou > Decimal("0"):
                return target_xrp / total_iou, total_depth
            return None, total_depth

        # Default: best qualifying offer rate
        xrp, iou = qualifying_offers[0]
        return xrp / iou, total_depth

    async def _get_sell_rate(
        self, currency: str, issuer: str, target_xrp: Optional[Decimal] = None,
    ) -> tuple[Optional[Decimal], Decimal]:
        """CLOB bid: volume-weighted XRP received per unit of IOU.

        When target_xrp is provided, calculates the effective rate to fill
        that amount across multiple book levels.  Otherwise returns the
        best qualifying offer rate (backward compatible).

        Returns (rate, depth_xrp) where depth_xrp is total qualifying
        liquidity on the book side.
        """
        request = BookOffers(
            taker_gets={"currency": "XRP"},
            taker_pays={"currency": currency, "issuer": issuer},
            limit=_BOOK_DEPTH,
        )
        result = await self.connection.send_request(request)
        if not result or not result.get("offers"):
            return None, Decimal("0")

        qualifying_offers: list[tuple[Decimal, Decimal]] = []  # (xrp_amount, iou_amount)

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
                qualifying_offers.append((xrp_amount, iou_amount))
            except (InvalidOperation, ArithmeticError, TypeError) as e:
                logger.debug(f"Sell book parse error: {e}")
                continue

        if not qualifying_offers:
            return None, Decimal("0")

        total_depth = sum(o[0] for o in qualifying_offers)

        # Volume-weighted rate for a specific trade size
        if target_xrp is not None and target_xrp > Decimal("0"):
            remaining = target_xrp
            total_iou = Decimal("0")
            for xrp_avail, iou_avail in qualifying_offers:
                rate = xrp_avail / iou_avail
                fill = min(remaining, xrp_avail)
                total_iou += fill / rate
                remaining -= fill
                if remaining <= Decimal("0"):
                    break
            if total_iou > Decimal("0"):
                return target_xrp / total_iou, total_depth
            return None, total_depth

        # Default: best qualifying offer rate
        xrp, iou = qualifying_offers[0]
        return xrp / iou, total_depth

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
          buy rate (ask)  = mid / (1 - fee)   -- you pay more
          sell rate (bid) = mid * (1 - fee)   -- you receive less

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
            fee_ratio = fee_bps / Decimal("100000")

            if isinstance(amount, str):
                xrp_reserve = Decimal(str(amount)) / DROPS_PER_XRP
                if not isinstance(amount2, dict):
                    return None
                iou_reserve = Decimal(str(amount2.get("value", "0")))
            elif isinstance(amount2, str):
                xrp_reserve = Decimal(str(amount2)) / DROPS_PER_XRP
                if not isinstance(amount, dict):
                    return None
                iou_reserve = Decimal(str(amount.get("value", "0")))
            else:
                return None

            if xrp_reserve <= Decimal("0") or iou_reserve <= Decimal("0"):
                return None

            mid_price = xrp_reserve / iou_reserve
            fee_factor = Decimal("1") - fee_ratio

            if fee_factor <= Decimal("0"):
                return None

            buy_rate = mid_price / fee_factor
            sell_rate = mid_price * fee_factor

            return (buy_rate, sell_rate)

        except (InvalidOperation, ArithmeticError, TypeError, KeyError) as e:
            logger.debug(f"AMM parse error for {currency}/{issuer[:8]}: {e}")
            return None

    # ------------------------------------------------------------------
    # Rate collection (parallel)
    # ------------------------------------------------------------------

    async def _collect_rates(self, trust_lines: list[dict]) -> list[IouRates]:
        """Fetch CLOB + AMM rates for all trust-lined IOUs in parallel.

        Each IOU requires 3 sequential RPC calls (buy book, sell book,
        amm_info).  All IOUs run concurrently via asyncio.gather.
        Rate limiting is handled at the connection level — XRPLConnection
        has a global semaphore that throttles all concurrent RPC calls
        to prevent 'slowDown' errors from public nodes.
        """

        async def _fetch_one(line: dict) -> IouRates:
            currency = line["currency"]
            issuer = line["account"]
            rates = IouRates(currency=currency, issuer=issuer)

            buy_result = await self._get_buy_rate(currency, issuer)
            rates.clob_buy, rates.clob_buy_depth_xrp = buy_result

            sell_result = await self._get_sell_rate(currency, issuer)
            rates.clob_sell, rates.clob_sell_depth_xrp = sell_result

            amm = await self._get_amm_rates(currency, issuer)
            if amm:
                rates.amm_buy, rates.amm_sell = amm

            return rates

        results = await asyncio.gather(
            *[_fetch_one(line) for line in trust_lines],
            return_exceptions=True,
        )

        good_rates: list[IouRates] = []
        for i, result in enumerate(results):
            if isinstance(result, IouRates):
                good_rates.append(result)
            elif isinstance(result, Exception):
                currency = trust_lines[i].get("currency", "?")
                logger.debug(f"Rate fetch failed for {currency}: {result}")

        return good_rates

    # ------------------------------------------------------------------
    # Multi-hop discovery via ripple_path_find
    # ------------------------------------------------------------------

    async def _discover_multi_hop(
        self, position_xrp: Decimal, volatility_factor: Decimal,
    ) -> list[Opportunity]:
        """Use ripple_path_find to discover circular arb paths.

        Sends XRP from our account back to ourselves, letting the XRPL
        server's pathfinding algorithm explore the full order book + AMM +
        trust line graph.  If source_amount < destination_amount, the path
        is profitable.

        This discovers multi-hop routes (3-6 intermediaries) that manual
        1-hop and 2-hop path construction would miss.
        """
        destination_drops = str(int(position_xrp * DROPS_PER_XRP))

        request = RipplePathFind(
            source_account=self.wallet_address,
            destination_account=self.wallet_address,
            destination_amount=destination_drops,
            source_currencies=[{"currency": "XRP"}],
        )

        result = await self.connection.send_request(request)
        if not result or "alternatives" not in result:
            return []

        opportunities: list[Opportunity] = []

        for alt in result["alternatives"]:
            try:
                source_amount = alt.get("source_amount")
                # source_amount for XRP is a string of drops
                if isinstance(source_amount, dict):
                    continue  # Non-XRP source, skip
                source_xrp = Decimal(str(source_amount)) / DROPS_PER_XRP
                output_xrp = position_xrp

                # Profitable only if we spend less than we receive
                if source_xrp >= output_xrp:
                    continue

                profit_ratio = calculate_profit(source_xrp, output_xrp, volatility_factor)
                if not is_profitable(source_xrp, output_xrp, volatility_factor):
                    continue

                profit_pct = profit_ratio * Decimal("100")

                if profit_pct > _MAX_PROFIT_PCT:
                    logger.debug(f"Rejected implausible multi-hop {profit_pct:.1f}%")
                    continue

                paths = alt.get("paths_computed", [])
                if not paths:
                    continue

                logger.info(
                    f"OPPORTUNITY [multi-hop]: {len(paths[0]) if paths else '?'} hops | "
                    f"{profit_pct:.4f}% profit | "
                    f"in={source_xrp:.6f} -> out={output_xrp:.6f} XRP"
                )

                opportunities.append(Opportunity(
                    input_xrp=source_xrp,
                    output_xrp=output_xrp,
                    profit_pct=profit_pct,
                    profit_ratio=profit_ratio,
                    paths=paths,
                    source_currency="XRP",
                ))

            except (InvalidOperation, ArithmeticError, TypeError, KeyError) as e:
                logger.debug(f"Multi-hop parse error: {e}")
                continue

        return opportunities

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
        """Two-hop path: XRP -> IOU(buy_issuer) -> IOU(sell_issuer) -> XRP."""
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
        threshold: Optional[Decimal] = None,
        sell_issuer: Optional[str] = None,
    ) -> Optional[Opportunity]:
        """Check if a buy/sell rate pair yields a profitable round-trip.

        `issuer` is always the buy-side issuer (leg 1 destination).
        `sell_issuer` is the rich-side issuer for cross-issuer arb;
        when None, it defaults to `issuer` (same-issuer arb).
        """
        if sell_rate <= buy_rate:
            return None

        output_xrp = position_xrp * sell_rate / buy_rate
        iou_amount = position_xrp / buy_rate  # IOU acquired in leg 1

        if output_xrp <= position_xrp:
            return None

        effective_threshold = threshold if threshold is not None else get_profit_threshold(currency)

        profit_ratio = calculate_profit(position_xrp, output_xrp, volatility_factor)
        if not is_profitable(position_xrp, output_xrp, volatility_factor, threshold=effective_threshold):
            return None

        profit_pct = profit_ratio * Decimal("100")

        if profit_pct > _MAX_PROFIT_PCT:
            logger.debug(
                f"Rejected implausible {profit_pct:.1f}% on {currency}/{issuer[:8]}... "
                f"(ask={buy_rate:.8f} bid={sell_rate:.8f})"
            )
            return None

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
            iou_currency=currency,
            buy_issuer=issuer,
            sell_issuer=sell_issuer if sell_issuer else issuer,
            iou_amount=iou_amount,
            paths=paths if paths is not None else self._build_path(currency, issuer),
            source_currency="XRP",
        )

    # ------------------------------------------------------------------
    # Opportunity evaluation (shared by scan and scan_pairs)
    # ------------------------------------------------------------------

    def _compute_position(
        self,
        account_balance: Decimal,
        buy_rate: Decimal,
        sell_rate: Decimal,
        volatility_factor: Decimal,
    ) -> Decimal:
        """Compute dynamic position size based on estimated profit quality.

        Does a quick profit estimate from the rates (no RPC needed), then
        feeds that into calculate_dynamic_position() to scale the trade
        size by opportunity quality and volatility.
        """
        if sell_rate <= buy_rate or buy_rate <= Decimal("0"):
            return Decimal("0")
        # Estimate gross profit ratio from rates alone
        estimated_ratio = (sell_rate - buy_rate) / buy_rate
        return calculate_dynamic_position(
            account_balance, estimated_ratio, volatility_factor,
        )

    def _evaluate_rates(
        self,
        all_rates: list[IouRates],
        account_balance: Decimal,
        volatility_factor: Decimal,
        volatility_tracker=None,
    ) -> list[Opportunity]:
        """Evaluate collected rates for same-issuer and cross-issuer opportunities.

        Uses dynamic position sizing: estimates profit quality from the
        spread, then scales position between MIN_POSITION_PCT and
        MAX_POSITION_PCT based on opportunity quality and volatility.
        """
        all_opps: list[Opportunity] = []

        # Same-issuer combined spreads
        for rates in all_rates:
            best_buy = rates.best_buy
            best_sell = rates.best_sell
            if best_buy is None or best_sell is None:
                continue

            if volatility_tracker is not None:
                vf = volatility_tracker.get_volatility(rates.currency)
            else:
                vf = volatility_factor

            position_xrp = self._compute_position(
                account_balance, best_buy, best_sell, vf,
            )
            if position_xrp <= Decimal("0"):
                continue

            opp = self._check_spread(
                rates.currency, rates.issuer,
                best_buy, best_sell,
                position_xrp, vf,
                label="same-issuer",
            )
            if opp:
                all_opps.append(opp)

        # Cross-issuer arbitrage
        groups: dict[str, list[IouRates]] = defaultdict(list)
        for rates in all_rates:
            if rates.best_buy is not None or rates.best_sell is not None:
                groups[rates.currency].append(rates)

        for currency, issuers in groups.items():
            if len(issuers) < 2:
                continue

            buy_candidates = [r for r in issuers if r.best_buy is not None]
            sell_candidates = [r for r in issuers if r.best_sell is not None]
            if not buy_candidates or not sell_candidates:
                continue

            cheapest = min(buy_candidates, key=lambda r: r.best_buy)
            richest = max(sell_candidates, key=lambda r: r.best_sell)

            if cheapest.issuer == richest.issuer:
                continue

            cross_path = self._build_cross_issuer_path(
                currency, cheapest.issuer, richest.issuer,
            )

            if volatility_tracker is not None:
                vf = volatility_tracker.get_volatility(currency)
            else:
                vf = volatility_factor

            position_xrp = self._compute_position(
                account_balance, cheapest.best_buy, richest.best_sell, vf,
            )
            if position_xrp <= Decimal("0"):
                continue

            opp = self._check_spread(
                currency, cheapest.issuer,
                cheapest.best_buy, richest.best_sell,
                position_xrp, vf,
                paths=cross_path,
                label=f"cross-issuer {cheapest.issuer[:8]}->{richest.issuer[:8]}",
                sell_issuer=richest.issuer,
            )
            if opp:
                all_opps.append(opp)

        return all_opps

    # ------------------------------------------------------------------
    # Targeted scan (event-driven, ~4-7 second latency)
    # ------------------------------------------------------------------

    async def scan_pairs(
        self,
        changed_currencies: set[str],
        account_balance: Decimal,
        volatility_tracker=None,
    ) -> list[Opportunity]:
        """Scan only the IOUs whose order books changed in the latest ledger.

        Called by the book_changes callback when rate changes are detected.
        This is the fast path: instead of scanning all 27 IOUs, we scan
        only the 1-5 that actually changed, achieving ~4-7 second reaction.

        Also includes cross-issuer checks when the changed currency has
        multiple issuers (needs rates for all issuers of that currency).

        Args:
            changed_currencies: Set of currency codes that had book changes.
            account_balance: Current XRP balance.
            volatility_tracker: Optional VolatilityTracker instance.
        """
        trust_lines = await self._fetch_trust_lines()
        if not trust_lines:
            return []

        # Find trust lines matching changed currencies.
        # For cross-issuer, we need ALL issuers of each changed currency.
        target_lines = [
            line for line in trust_lines
            if line["currency"] in changed_currencies
        ]

        if not target_lines:
            return []

        logger.debug(
            f"Targeted scan: {len(target_lines)} IOUs for "
            f"currencies {changed_currencies}"
        )

        # Fetch rates only for affected pairs (parallel)
        all_rates = await self._collect_rates(target_lines)

        vf = (
            volatility_tracker.get_global_volatility()
            if volatility_tracker else Decimal("0")
        )

        all_opps = self._evaluate_rates(
            all_rates, account_balance, vf,
            volatility_tracker=volatility_tracker,
        )

        if all_opps:
            logger.info(
                f"Targeted scan found {len(all_opps)} opportunities "
                f"in {changed_currencies}"
            )

        return _deduplicate_opportunities(all_opps)

    # ------------------------------------------------------------------
    # Full scan (periodic fallback + multi-hop discovery)
    # ------------------------------------------------------------------

    async def scan(
        self,
        account_balance: Decimal,
        volatility_factor: Decimal = Decimal("0"),
        volatility_tracker=None,
    ) -> list[Opportunity]:
        """Full multi-strategy scan: CLOB, AMM, cross-issuer, and multi-hop.

        Phase 1: Collect CLOB + AMM rates for all 27 IOUs (parallel RPC).
        Phase 2: Same-issuer -- best buy vs best sell across venues.
        Phase 3: Cross-issuer -- cheapest buy vs best sell across issuers.
        Phase 4: Multi-hop -- ripple_path_find for circular arb discovery.

        Position sizing is dynamic: scaled by opportunity quality and
        volatility via calculate_dynamic_position().

        This is the periodic fallback scan (every SCAN_INTERVAL ledgers).
        For real-time response, scan_pairs() handles changed-pair scanning.
        """
        trust_lines = await self._fetch_trust_lines()

        if not trust_lines:
            logger.warning("No trust lines found -- nothing to scan")
            return []

        # Phase 1: Collect all rates (parallel)
        all_rates = await self._collect_rates(trust_lines)

        rated_count = sum(1 for r in all_rates if r.best_buy and r.best_sell)
        amm_count = sum(1 for r in all_rates if r.amm_buy is not None)

        # Determine global volatility
        if volatility_tracker is not None:
            vf = volatility_tracker.get_global_volatility()
        else:
            vf = volatility_factor

        # Phases 2-3: Same-issuer and cross-issuer (dynamic position sizing)
        all_opps = self._evaluate_rates(
            all_rates, account_balance, vf,
            volatility_tracker=volatility_tracker,
        )

        # Phase 4: Multi-hop discovery via ripple_path_find
        # Probe at 5% of balance (middle of dynamic range)
        mid_position = account_balance * Decimal("0.05")
        if mid_position > Decimal("0"):
            multi_hop_opps = await self._discover_multi_hop(mid_position, vf)
            all_opps.extend(multi_hop_opps)

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
