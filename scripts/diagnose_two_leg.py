"""Diagnostic: test the two-leg pathfinder against live XRPL mainnet.

Connects to XRPL, fetches trust lines, and runs buy/sell probes for
each IOU to show effective round-trip rates.  No transactions are sent.

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


PROBE_XRP = Decimal("1")  # Probe with 1 XRP round-trip


async def main():
    if not XRPL_SECRET:
        print("ERROR: XRPL_SECRET not set in .env")
        sys.exit(1)

    wallet = Wallet.from_seed(XRPL_SECRET)
    print(f"Wallet:  {wallet.address}")
    print(f"Node:    {XRPL_WS_URL}")
    print(f"Probe:   {PROBE_XRP} XRP round-trip")
    print(f"Threshold: {PROFIT_THRESHOLD * 100}%")
    print()

    connection = XRPLConnection()
    pathfinder = PathFinder(connection, wallet.address)

    # Connect and run diagnostics inside the WebSocket context
    async with __import__("xrpl").asyncio.clients.AsyncWebsocketClient(XRPL_WS_URL) as client:
        connection.client = client
        connection.connected = True

        # Fetch trust lines
        lines = await pathfinder._fetch_trust_lines()
        print(f"Trust lines: {len(lines)}")
        print("=" * 80)

        results = []

        for line in lines:
            currency = line["currency"]
            issuer = line["account"]
            # Truncate for display
            display_currency = currency if len(currency) <= 5 else currency[:8] + "..."
            display_issuer = issuer[:12] + "..."

            # Sell probe: how much IOU to get PROBE_XRP back?
            sell_iou = await pathfinder._probe_sell_cost(currency, issuer, PROBE_XRP)
            if sell_iou is None:
                print(f"  {display_currency:>12} / {display_issuer}  SELL PROBE: no path")
                results.append((display_currency, display_issuer, None, None, None))
                continue

            # Buy probe: how much XRP to buy that IOU?
            buy_xrp = await pathfinder._probe_buy_cost(currency, issuer, sell_iou)
            if buy_xrp is None:
                print(f"  {display_currency:>12} / {display_issuer}  BUY PROBE:  no path (sell needed {sell_iou} IOU)")
                results.append((display_currency, display_issuer, sell_iou, None, None))
                continue

            # Compute round-trip
            if buy_xrp > Decimal("0"):
                profit_ratio = calculate_profit(buy_xrp, PROBE_XRP)
                profit_pct = profit_ratio * Decimal("100")
                profitable = profit_ratio > PROFIT_THRESHOLD

                marker = " *** OPPORTUNITY ***" if profitable else ""
                print(
                    f"  {display_currency:>12} / {display_issuer}  "
                    f"buy={buy_xrp:.6f} XRP  sell={PROBE_XRP} XRP  "
                    f"via {sell_iou:.6f} IOU  "
                    f"profit={profit_pct:+.4f}%{marker}"
                )
                results.append((display_currency, display_issuer, sell_iou, buy_xrp, profit_pct))
            else:
                print(f"  {display_currency:>12} / {display_issuer}  buy=0 XRP (invalid)")
                results.append((display_currency, display_issuer, sell_iou, Decimal("0"), None))

        # Summary
        print()
        print("=" * 80)
        valid = [r for r in results if r[4] is not None]
        if valid:
            best = max(valid, key=lambda r: r[4])
            worst = min(valid, key=lambda r: r[4])
            profitable = [r for r in valid if r[4] > PROFIT_THRESHOLD * 100]

            print(f"Probed: {len(lines)} IOUs, {len(valid)} returned rates")
            print(f"Best:   {best[0]} / {best[1]}  {best[4]:+.4f}%")
            print(f"Worst:  {worst[0]} / {worst[1]}  {worst[4]:+.4f}%")
            print(f"Above threshold ({PROFIT_THRESHOLD * 100}%): {len(profitable)}")
        else:
            print("No valid rates returned from any IOU.")


if __name__ == "__main__":
    asyncio.run(main())
