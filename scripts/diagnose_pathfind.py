"""Diagnostic script to test ripple_path_find directly.

Tests whether the XRPL node returns any path alternatives for
XRP->IOU->XRP circular arbitrage. Helps debug zero-opportunity issues.

Usage:
    python scripts/diagnose_pathfind.py

    # On VPS:
    sudo -u xrplbot /opt/xrplbot/venv/bin/python scripts/diagnose_pathfind.py
"""

import asyncio
import json
import sys
from decimal import Decimal

import requests
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import RipplePathFind, AccountLines
from xrpl.wallet import Wallet

# Load .env from project root
sys.path.insert(0, ".")
from src.config import XRPL_SECRET, XRPL_WS_URL, XRPL_RPC_URL

DROPS_PER_XRP = Decimal("1000000")


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


async def main():
    if not XRPL_SECRET:
        print("ERROR: XRPL_SECRET not set in .env")
        sys.exit(1)

    wallet = Wallet.from_seed(XRPL_SECRET)
    print(f"Wallet:     {wallet.address}")
    print(f"WS URL:     {XRPL_WS_URL}")
    print(f"RPC URL:    {XRPL_RPC_URL}")

    # ── Step 1: Check trust lines ──────────────────────────────
    section("1. Trust Lines Check")

    async with AsyncWebsocketClient(XRPL_WS_URL) as client:
        # Check trust lines via WebSocket
        lines_req = AccountLines(account=wallet.address)
        lines_resp = await client.request(lines_req)

        if lines_resp.is_successful():
            lines = lines_resp.result.get("lines", [])
            print(f"Trust lines found: {len(lines)}")
            for i, line in enumerate(lines[:5]):
                print(f"  [{i+1}] {line.get('currency', '?')} "
                      f"/ {line.get('account', '?')[:12]}... "
                      f"limit={line.get('limit', '?')} "
                      f"no_ripple={'yes' if line.get('no_ripple') else 'NO'}")
            if len(lines) > 5:
                print(f"  ... and {len(lines) - 5} more")
        else:
            print(f"ERROR fetching trust lines: {lines_resp.result}")
            print("Cannot continue without trust lines.")
            return

        if len(lines) == 0:
            print("\nNO TRUST LINES! ripple_path_find cannot route through IOUs.")
            print("Run: python scripts/setup_trust_lines.py")
            return

        # ── Step 2: Test ripple_path_find at multiple amounts ──────
        section("2. ripple_path_find Tests (via WebSocket)")

        test_amounts = [
            Decimal("1"),      # Small
            Decimal("5"),      # Medium
            Decimal("10"),     # Larger
            Decimal("50"),     # Big
        ]

        for amount in test_amounts:
            dest_drops = str(int(amount * DROPS_PER_XRP))
            req = RipplePathFind(
                source_account=wallet.address,
                destination_account=wallet.address,
                destination_amount=dest_drops,
                source_currencies=[{"currency": "XRP"}],
            )

            print(f"--- Testing {amount} XRP (dest={dest_drops} drops) ---")
            resp = await client.request(req)

            if not resp.is_successful():
                print(f"  FAILED: {resp.result}")
                continue

            result = resp.result
            alts = result.get("alternatives", [])
            print(f"  Alternatives returned: {len(alts)}")

            if alts:
                for j, alt in enumerate(alts[:3]):
                    source_amount = alt.get("source_amount", "?")
                    paths = alt.get("paths_computed", [])

                    # Parse source amount
                    if isinstance(source_amount, str):
                        src_xrp = Decimal(source_amount) / DROPS_PER_XRP
                        profit_pct = ((amount - src_xrp) / src_xrp) * 100
                        print(f"  Alt {j+1}: source={src_xrp} XRP, "
                              f"dest={amount} XRP, "
                              f"profit={profit_pct:.4f}%, "
                              f"paths={len(paths)}")
                    elif isinstance(source_amount, dict):
                        print(f"  Alt {j+1}: source={source_amount} (non-XRP), "
                              f"paths={len(paths)}")
                    else:
                        print(f"  Alt {j+1}: source={source_amount} (unknown type)")

                    # Show first path details
                    if paths and len(paths) > 0:
                        for p_idx, path in enumerate(paths[:2]):
                            hops = []
                            for hop in path:
                                if isinstance(hop, dict):
                                    curr = hop.get("currency", "?")
                                    acct = hop.get("account", "")
                                    if acct:
                                        hops.append(f"{curr}@{acct[:8]}...")
                                    else:
                                        hops.append(curr)
                            print(f"         Path {p_idx+1}: {' -> '.join(hops) if hops else path}")
            else:
                # Print the raw result keys to understand what we got
                print(f"  Result keys: {list(result.keys())}")
                # Check for any error indicators
                if "error" in result:
                    print(f"  Error: {result['error']}")
                if "error_message" in result:
                    print(f"  Error msg: {result['error_message']}")

            print()

        # ── Step 3: Test with IOU destination ──────────────────────
        section("3. Reverse Test: IOU destination (XRP source)")
        print("Testing if paths exist for XRP -> specific IOUs...")
        print("(This tests if the DEX has liquidity we can route through)\n")

        # Pick a few high-liquidity IOUs from trust lines
        test_ious = []
        for line in lines:
            currency = line.get("currency", "")
            issuer = line.get("account", "")
            if currency in ("USD", "EUR", "BTC"):
                test_ious.append((currency, issuer, "1"))  # 1 unit
            if len(test_ious) >= 3:
                break

        # Also test with RLUSD hex code if we have it
        for line in lines:
            currency = line.get("currency", "")
            if currency == "524C555344000000000000000000000000000000":
                test_ious.append((currency, line["account"], "1"))
                break

        if not test_ious:
            # Fallback: use first 3 trust lines
            for line in lines[:3]:
                test_ious.append((line["currency"], line["account"], "1"))

        for currency, issuer, amount in test_ious:
            display_name = currency if len(currency) <= 3 else currency[:8] + "..."
            req = RipplePathFind(
                source_account=wallet.address,
                destination_account=wallet.address,
                destination_amount={
                    "currency": currency,
                    "issuer": issuer,
                    "value": amount,
                },
                source_currencies=[{"currency": "XRP"}],
            )

            print(f"--- XRP -> {amount} {display_name} ({issuer[:12]}...) ---")
            resp = await client.request(req)

            if not resp.is_successful():
                print(f"  FAILED: {resp.result}")
                continue

            result = resp.result
            alts = result.get("alternatives", [])
            print(f"  Alternatives: {len(alts)}")
            for j, alt in enumerate(alts[:2]):
                src = alt.get("source_amount", "?")
                if isinstance(src, str):
                    print(f"  Alt {j+1}: costs {Decimal(src)/DROPS_PER_XRP} XRP")
                else:
                    print(f"  Alt {j+1}: costs {src}")
            print()

    # ── Step 4: Test via HTTP RPC (comparison) ─────────────────
    section("4. ripple_path_find via HTTP RPC (comparison)")

    payload = {
        "method": "ripple_path_find",
        "params": [{
            "source_account": wallet.address,
            "destination_account": wallet.address,
            "destination_amount": "5000000",
            "source_currencies": [{"currency": "XRP"}],
        }],
    }

    try:
        resp = requests.post(XRPL_RPC_URL, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        status = result.get("status", "?")
        alts = result.get("alternatives", [])
        print(f"Status: {status}")
        print(f"Alternatives: {len(alts)}")

        if "error" in result:
            print(f"Error: {result['error']}")
            print(f"Error msg: {result.get('error_message', '')}")

        if alts:
            for j, alt in enumerate(alts[:3]):
                src = alt.get("source_amount", "?")
                print(f"  Alt {j+1}: source_amount={src}")

        # Check for Clio header
        server = resp.headers.get("server", "")
        if "clio" in server.lower():
            print(f"\nWARNING: Response from Clio server ({server})")
            print("Clio doesn't fully support ripple_path_find!")
        else:
            print(f"\nServer: {server or 'not reported'}")

    except Exception as e:
        print(f"HTTP RPC error: {e}")

    # ── Summary ────────────────────────────────────────────────
    section("Summary")
    print("If all tests returned 0 alternatives:")
    print("  1. The XRPL DEX may have no profitable XRP circular paths right now")
    print("  2. Arbitrage on XRPL tends to be rare and short-lived")
    print("  3. The bot's approach is correct — it just needs market inefficiency")
    print()
    print("If IOU destination tests (Step 3) returned alternatives but")
    print("XRP-to-XRP tests (Step 2) didn't:")
    print("  -> Circular XRP arbitrage paths are rare; consider also scanning")
    print("     for cross-currency opportunities or using book_offers API")
    print()
    print("If HTTP RPC (Step 4) shows Clio server:")
    print("  -> Switch XRPL_RPC_URL to https://xrplcluster.com or s2.ripple.com:51234")


if __name__ == "__main__":
    asyncio.run(main())
