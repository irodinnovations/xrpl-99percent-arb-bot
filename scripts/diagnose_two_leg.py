"""Diagnostic: CLOB + AMM + cross-issuer rate analysis on live XRPL mainnet.

Connects to XRPL, fetches trust lines, and queries CLOB order books and
AMM pools for each IOU.  Shows combined best rates and cross-issuer
opportunities.  No transactions are sent.

Usage:
    python scripts/diagnose_two_leg.py
    # On VPS:
    sudo -u xrplbot /opt/xrplbot/venv/bin/python scripts/diagnose_two_leg.py
"""

import asyncio
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, ".")
from src.config import XRPL_SECRET, XRPL_WS_URL
from src.connection import XRPLConnection
from src.pathfinder import PathFinder, IouRates, DROPS_PER_XRP
from src.profit_math import calculate_profit, PROFIT_THRESHOLD

from xrpl.wallet import Wallet


PROBE_XRP = Decimal("1")


async def main():
    if not XRPL_SECRET:
        print("ERROR: XRPL_SECRET not set in .env")
        sys.exit(1)

    wallet = Wallet.from_seed(XRPL_SECRET)
    print(f"Wallet:    {wallet.address}")
    print(f"Node:      {XRPL_WS_URL}")
    print(f"Position:  {PROBE_XRP} XRP")
    print(f"Threshold: {PROFIT_THRESHOLD * 100}%")
    print()

    connection = XRPLConnection()
    pathfinder = PathFinder(connection, wallet.address)

    async with __import__("xrpl").asyncio.clients.AsyncWebsocketClient(XRPL_WS_URL) as client:
        connection.client = client
        connection.connected = True

        lines = await pathfinder._fetch_trust_lines()
        print(f"Trust lines: {len(lines)}")

        # --- Phase 1: Collect all rates ---
        all_rates = await pathfinder._collect_rates(lines)
        amm_count = sum(1 for r in all_rates if r.amm_buy is not None)

        # --- Phase 2: Same-issuer table ---
        print()
        print("=" * 110)
        print("SAME-ISSUER RATES (best of CLOB + AMM)")
        print(f"  {'IOU':>14} / {'Issuer':<15}  {'Best Ask':>12}  {'Best Bid':>12}  {'Spread':>10}  {'Profit':>10}  {'AMM':>5}")
        print("-" * 110)

        for rates in all_rates:
            dc = rates.currency if len(rates.currency) <= 5 else rates.currency[:8] + "..."
            di = rates.issuer[:12] + "..."
            has_amm = "yes" if rates.amm_buy is not None else ""
            bb = rates.best_buy
            bs = rates.best_sell

            if bb is None or bs is None:
                status = "partial" if (bb or bs) else "no book"
                print(f"  {dc:>14} / {di:<15}  {status:>30}  {has_amm:>42}")
                continue

            spread_pct = ((bs - bb) / bb) * Decimal("100")
            output = PROBE_XRP * bs / bb
            profit_pct = calculate_profit(PROBE_XRP, output) * Decimal("100")
            marker = " ***" if profit_pct > PROFIT_THRESHOLD * 100 else ""

            print(
                f"  {dc:>14} / {di:<15}"
                f"  {bb:>12.8f}  {bs:>12.8f}"
                f"  {spread_pct:>+9.4f}%  {profit_pct:>+9.4f}%{marker}"
                f"  {has_amm:>5}"
            )

        # --- Phase 3: Cross-issuer analysis ---
        groups: dict[str, list[IouRates]] = defaultdict(list)
        for r in all_rates:
            if r.best_buy is not None or r.best_sell is not None:
                groups[r.currency].append(r)

        cross_pairs = {k: v for k, v in groups.items() if len(v) >= 2}

        if cross_pairs:
            print()
            print("=" * 110)
            print("CROSS-ISSUER ANALYSIS")
            print("-" * 110)

            for currency, issuers in cross_pairs.items():
                dc = currency if len(currency) <= 5 else currency[:8] + "..."
                buy_candidates = [r for r in issuers if r.best_buy is not None]
                sell_candidates = [r for r in issuers if r.best_sell is not None]

                if not buy_candidates or not sell_candidates:
                    continue

                cheapest = min(buy_candidates, key=lambda r: r.best_buy)
                richest = max(sell_candidates, key=lambda r: r.best_sell)

                print(f"  {dc}: {len(issuers)} issuers")
                for r in issuers:
                    tag = ""
                    if r.issuer == cheapest.issuer:
                        tag += " <-- cheapest buy"
                    if r.issuer == richest.issuer:
                        tag += " <-- best sell"
                    bb_str = f"{r.best_buy:.8f}" if r.best_buy else "N/A"
                    bs_str = f"{r.best_sell:.8f}" if r.best_sell else "N/A"
                    print(f"    {r.issuer[:16]}...  ask={bb_str}  bid={bs_str}{tag}")

                if cheapest.issuer != richest.issuer:
                    cross_output = PROBE_XRP * richest.best_sell / cheapest.best_buy
                    cross_profit = calculate_profit(PROBE_XRP, cross_output) * Decimal("100")
                    marker = " *** OPPORTUNITY ***" if cross_profit > PROFIT_THRESHOLD * 100 else ""
                    print(
                        f"    Cross-issuer: buy {cheapest.issuer[:12]}... @ {cheapest.best_buy:.8f}"
                        f" → sell {richest.issuer[:12]}... @ {richest.best_sell:.8f}"
                        f"  profit={cross_profit:+.4f}%{marker}"
                    )
                else:
                    print(f"    Same issuer best on both sides — no cross-issuer opportunity")
                print()

        # --- Summary ---
        print("=" * 110)
        rated = [r for r in all_rates if r.best_buy and r.best_sell]
        print(f"Total: {len(lines)} IOUs | {len(rated)} with books | {amm_count} with AMM pools")

        if rated:
            best_same = max(rated, key=lambda r: r.best_sell / r.best_buy)
            bs_profit = (best_same.best_sell / best_same.best_buy - 1) * 100
            dc = best_same.currency if len(best_same.currency) <= 5 else best_same.currency[:8] + "..."
            print(f"Best same-issuer spread: {dc} / {best_same.issuer[:12]}... ({bs_profit:+.4f}% gross)")

        if cross_pairs:
            best_cross_profit = Decimal("-999")
            best_cross_label = ""
            for currency, issuers in cross_pairs.items():
                buys = [r for r in issuers if r.best_buy]
                sells = [r for r in issuers if r.best_sell]
                if not buys or not sells:
                    continue
                ch = min(buys, key=lambda r: r.best_buy)
                ri = max(sells, key=lambda r: r.best_sell)
                if ch.issuer == ri.issuer:
                    continue
                p = (ri.best_sell / ch.best_buy - 1) * 100
                if p > best_cross_profit:
                    best_cross_profit = p
                    dc = currency if len(currency) <= 5 else currency[:8] + "..."
                    best_cross_label = f"{dc}: {ch.issuer[:12]}→{ri.issuer[:12]}"

            if best_cross_label:
                print(f"Best cross-issuer spread: {best_cross_label} ({best_cross_profit:+.4f}% gross)")

        print(f"Threshold: {PROFIT_THRESHOLD * 100}% net (after fees + slippage)")


if __name__ == "__main__":
    asyncio.run(main())
