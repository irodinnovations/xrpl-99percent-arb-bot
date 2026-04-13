"""Diagnostic: test order-book rate discovery against live XRPL mainnet.

Connects to XRPL, fetches trust lines, and queries buy/sell order books
for each IOU to show bid/ask spreads.  No transactions are sent.

Usage:
    python scripts/diagnose_two_leg.py
    # On VPS:
    sudo -u xrplbot /opt/xrplbot/venv/bin/python scripts/diagnose_two_leg.py
"""

import asyncio
import sys
from decimal import Decimal

sys.path.insert(0, ".")
from src.config import XRPL_SECRET, XRPL_WS_URL
from src.connection import XRPLConnection
from src.pathfinder import PathFinder, DROPS_PER_XRP
from src.profit_math import calculate_profit, PROFIT_THRESHOLD

from xrpl.wallet import Wallet


PROBE_XRP = Decimal("1")  # Position size for profit calculation


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

        # Fetch trust lines
        lines = await pathfinder._fetch_trust_lines()
        print(f"Trust lines: {len(lines)}")
        print("=" * 90)
        print(f"  {'IOU':>14} / {'Issuer':<15}  {'Ask (buy)':>12}  {'Bid (sell)':>12}  {'Spread':>10}  {'Profit':>10}")
        print("-" * 90)

        results = []

        for line in lines:
            currency = line["currency"]
            issuer = line["account"]
            display_currency = currency if len(currency) <= 5 else currency[:8] + "..."
            display_issuer = issuer[:12] + "..."

            buy_rate = await pathfinder._get_buy_rate(currency, issuer)
            sell_rate = await pathfinder._get_sell_rate(currency, issuer)

            if buy_rate is None and sell_rate is None:
                print(f"  {display_currency:>14} / {display_issuer:<15}  {'no book':>12}  {'no book':>12}")
                results.append((display_currency, display_issuer, None, None, None))
                continue

            if buy_rate is None:
                print(f"  {display_currency:>14} / {display_issuer:<15}  {'no book':>12}  {sell_rate:>12.8f}")
                results.append((display_currency, display_issuer, None, sell_rate, None))
                continue

            if sell_rate is None:
                print(f"  {display_currency:>14} / {display_issuer:<15}  {buy_rate:>12.8f}  {'no book':>12}")
                results.append((display_currency, display_issuer, buy_rate, None, None))
                continue

            # Both rates available — compute spread and profit
            spread_pct = ((sell_rate - buy_rate) / buy_rate) * Decimal("100")

            # Round-trip profit at PROBE_XRP position
            output_xrp = PROBE_XRP * sell_rate / buy_rate
            profit_ratio = calculate_profit(PROBE_XRP, output_xrp)
            profit_pct = profit_ratio * Decimal("100")
            profitable = profit_ratio > PROFIT_THRESHOLD

            marker = " ***" if profitable else ""
            print(
                f"  {display_currency:>14} / {display_issuer:<15}"
                f"  {buy_rate:>12.8f}  {sell_rate:>12.8f}"
                f"  {spread_pct:>+9.4f}%  {profit_pct:>+9.4f}%{marker}"
            )
            results.append((display_currency, display_issuer, buy_rate, sell_rate, profit_pct))

        # Summary
        print()
        print("=" * 90)
        with_both = [r for r in results if r[2] is not None and r[3] is not None and r[4] is not None]
        with_any = [r for r in results if r[2] is not None or r[3] is not None]

        print(f"Total: {len(lines)} IOUs | {len(with_any)} with order books | {len(with_both)} with both sides")

        if with_both:
            best = max(with_both, key=lambda r: r[4])
            worst = min(with_both, key=lambda r: r[4])
            profitable = [r for r in with_both if r[4] > PROFIT_THRESHOLD * 100]

            print(f"Best:  {best[0]} / {best[1]}  {best[4]:+.4f}%")
            print(f"Worst: {worst[0]} / {worst[1]}  {worst[4]:+.4f}%")
            print(f"Above threshold ({PROFIT_THRESHOLD * 100}%): {len(profitable)}")

            if not profitable:
                print()
                print("No opportunities above threshold right now.")
                print("This is normal — the bot continuously scans and will catch")
                print("transient mispricings when they appear.")
        else:
            print("No IOUs had both buy and sell order books.")


if __name__ == "__main__":
    asyncio.run(main())
