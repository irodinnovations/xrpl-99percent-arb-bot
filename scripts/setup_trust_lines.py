"""One-time trust line setup for XRPL arbitrage bot.

Sets up trust lines to Tier 1-3 tokens so ripple_path_find can route
XRP->IOU->XRP loops through intermediate tokens. Each trust line locks
1 XRP as owner reserve (returned if the trust line is removed later).

Usage:
    # Preview what would be set (no transactions submitted):
    python scripts/setup_trust_lines.py --dry-run

    # Set all trust lines on mainnet:
    python scripts/setup_trust_lines.py

    # On VPS:
    sudo -u xrplbot /opt/xrplbot/venv/bin/python scripts/setup_trust_lines.py

Idempotent: safe to re-run. Setting a trust line that already exists
is a no-op on XRPL (TrustSet with same limit succeeds without change).

Requires XRPL_SECRET in .env (same as bot).
"""

import argparse
import sys
import time
from decimal import Decimal

import requests
from xrpl.core.binarycodec import encode as xrpl_encode, encode_for_signing
from xrpl.core.keypairs import sign as keypairs_sign
from xrpl.wallet import Wallet

# Load .env from project root
sys.path.insert(0, ".")
from src.config import XRPL_SECRET, XRPL_RPC_URL


# ---------------------------------------------------------------------------
# Trust line definitions — Tiers 1-3
# ---------------------------------------------------------------------------
# Each entry: (display_name, currency_code_for_xrpl, issuer_address, limit)
#
# currency_code_for_xrpl:
#   - 3-char codes used as-is (e.g., "USD", "EUR")
#   - Longer names use 40-char uppercase hex encoding per XRPL spec
#
# limit: maximum amount you're willing to hold. Set high for routing —
#   the bot never accumulates tokens, it routes through them in a single tx.
# ---------------------------------------------------------------------------

TRUST_LINES = [
    # --- Tier 1: Major stablecoins & fiat (8) ---
    ("RLUSD (Ripple)",       "524C555344000000000000000000000000000000", "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De", "1000000"),
    ("USDC (Circle)",        "5553444300000000000000000000000000000000", "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE", "1000000"),
    ("USD (Bitstamp)",       "USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", "1000000"),
    ("USD (GateHub)",        "USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq", "1000000"),
    ("EUR (GateHub)",        "EUR", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq", "1000000"),
    ("BTC (Bitstamp)",       "BTC", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", "100"),
    ("BTC (GateHub Fifth)",  "BTC", "rchGBxcD1A1C2tdxF6papQYZ8kjRKMYcL", "100"),
    ("ETH (GateHub Fifth)",  "ETH", "rcA8X3TVMST1n3CJeAdGk1RdRCHii7N2h", "1000"),

    # --- Tier 2: XRPL-native tokens (6) ---
    ("SOLO (Sologenic)",     "534F4C4F00000000000000000000000000000000", "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz", "10000000"),
    ("CORE (Coreum)",        "434F524500000000000000000000000000000000", "rcoreNywaoz2ZCQ8Lg2EbSLnGuRBmun6D",  "10000000"),
    ("CSC (CasinoCoin)",     "CSC", "rCSCManTZ8ME9EoLrSHHYKW8PPwWMgkwr", "100000000"),
    ("FUZZY",                "46555A5A59000000000000000000000000000000", "rhCAT4hRdi2Y9puNdkpMzxrdKa5wkppR62", "100000000"),
    ("PHNIX",                "50484E4958000000000000000000000000000000", "rDFXbW2ZZCG5WgPtqwNiA2xZokLMm9ivmN", "100000000"),
    ("CNY (Ripple Fox)",     "CNY", "rKiCet8SdvWxPXnAgYarFUXMh1zCPz432Y", "10000000"),

    # --- Tier 3: Moderate liquidity (13) ---
    ("REAL",                 "5245414C00000000000000000000000000000000", "rKVyXn1AhqMTvNA9hS6XkFjQNn2VE8Nz88", "100000000"),
    ("ARMY",                 "41524D5900000000000000000000000000000000", "rGG3wQ4kUzd7Jnmk1n5NWPZjjut62kCBfC", "100000000"),
    ("XRdoge",               "5852646F67650000000000000000000000000000", "rLqUC2eCPohYvJCEBJ77eCCqVL2uEiczjA", "1000000000"),
    ("ELS (Aesthetes)",      "ELS", "rHXuEaRYnnJHbDeuBH5w8yPh5uwNVh5zAg", "100000000"),
    ("GBP (GateHub)",        "GBP", "r4GN9eEoz9K4BhMQXe4H1eYNtvtkwGdt8g", "1000000"),
    ("USDT (GateHub)",       "5553445400000000000000000000000000000000", "rcvxE9PS9YBwxtGg1qNeewV6ZB3wGubZq",  "1000000"),
    ("USDC (GateHub)",       "5553444300000000000000000000000000000000", "rcEGREd8NmkKRE8GE424sksyt1tJVFZwu",  "1000000"),
    ("XAH (Xahau)",          "XAH", "rswh1fvyLqHizBS2awu1vs6QcmwTBd9qiv", "10000000"),
    ("FLR (GateHub)",        "FLR", "rcxJwVnftZzXqyH9YheB8TgeiZUhNo1Eu",  "10000000"),
    ("SGB (GateHub)",        "SGB", "rctArjqVvTHihekzDeecKo6mkTYTUSBNc",  "10000000"),
    ("WXRP (GateHub)",       "5758525000000000000000000000000000000000", "rEa5QY8tdbjgitLyfKF1E5Qx3VGgvbUhB3", "10000000"),
    ("BXE (Banxchange)",     "BXE", "rM1J2Mc2eCSFpCz5QXxhDG2KWkGQWgy87r", "100000000"),
    ("Equilibrium",          "457175696C69627269756D000000000000000000", "rpakCr61Q92abPXJnVboKENmpKssWyHpwu", "10000000"),
]


def get_account_info(wallet_address: str) -> dict:
    """Fetch account info (Sequence, balance) from XRPL."""
    payload = {
        "method": "account_info",
        "params": [{"account": wallet_address, "ledger_index": "current"}],
    }
    resp = requests.post(XRPL_RPC_URL, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {})


def get_existing_trust_lines(wallet_address: str) -> set[tuple[str, str]]:
    """Fetch existing trust lines as a set of (currency, issuer) tuples."""
    payload = {
        "method": "account_lines",
        "params": [{"account": wallet_address}],
    }
    resp = requests.post(XRPL_RPC_URL, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json().get("result", {})
    lines = result.get("lines", [])
    return {(line["currency"], line["account"]) for line in lines}


def build_trust_set_tx(
    wallet_address: str,
    currency: str,
    issuer: str,
    limit: str,
    sequence: int,
    current_ledger: int,
) -> dict:
    """Build a raw TrustSet transaction dict."""
    return {
        "TransactionType": "TrustSet",
        "Account": wallet_address,
        "LimitAmount": {
            "currency": currency,
            "issuer": issuer,
            "value": limit,
        },
        "Sequence": sequence,
        "Fee": "12",
        "LastLedgerSequence": current_ledger + 20,  # ~80s window, generous for sequential
        "Flags": 0,
    }


def sign_and_submit(tx_dict: dict, wallet: Wallet) -> dict:
    """Client-side sign and submit a transaction. Returns submit result."""
    tx_dict["SigningPubKey"] = wallet.public_key
    encoded_for_signing = encode_for_signing(tx_dict)
    signature = keypairs_sign(
        bytes.fromhex(encoded_for_signing), wallet.private_key
    )
    tx_dict["TxnSignature"] = signature
    tx_blob = xrpl_encode(tx_dict)

    payload = {
        "method": "submit",
        "params": [{"tx_blob": tx_blob}],
    }
    resp = requests.post(XRPL_RPC_URL, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {})


def main():
    parser = argparse.ArgumentParser(
        description="Set up XRPL trust lines for arbitrage routing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview trust lines without submitting transactions.",
    )
    args = parser.parse_args()

    if not XRPL_SECRET:
        print("ERROR: XRPL_SECRET not set in .env")
        sys.exit(1)

    wallet = Wallet.from_seed(XRPL_SECRET)
    print(f"Wallet: {wallet.address}")
    print(f"Trust lines to set: {len(TRUST_LINES)}")
    print(f"XRP reserve cost: {len(TRUST_LINES)} XRP (locked, not spent)")
    print()

    if args.dry_run:
        print("=== DRY RUN — no transactions will be submitted ===\n")
        for i, (name, currency, issuer, limit) in enumerate(TRUST_LINES, 1):
            print(f"  [{i:2d}/{len(TRUST_LINES)}] {name}")
            print(f"         Currency: {currency}")
            print(f"         Issuer:   {issuer}")
            print(f"         Limit:    {limit}")
            print()
        print(f"Total: {len(TRUST_LINES)} trust lines, {len(TRUST_LINES)} XRP reserve")
        return

    # Check existing trust lines to report which are new
    print("Checking existing trust lines...")
    existing = get_existing_trust_lines(wallet.address)
    print(f"Found {len(existing)} existing trust lines\n")

    # Fetch account info for sequence number
    acct_info = get_account_info(wallet.address)
    if "account_data" not in acct_info:
        print(f"ERROR: Could not fetch account info: {acct_info}")
        sys.exit(1)

    sequence = acct_info["account_data"]["Sequence"]
    current_ledger = acct_info.get("ledger_current_index", 0)
    balance_drops = int(acct_info["account_data"]["Balance"])
    balance_xrp = Decimal(balance_drops) / Decimal("1000000")
    print(f"Balance: {balance_xrp} XRP")
    print(f"Starting sequence: {sequence}")
    print()

    success_count = 0
    skip_count = 0
    fail_count = 0

    for i, (name, currency, issuer, limit) in enumerate(TRUST_LINES, 1):
        # Check if this trust line already exists
        if (currency, issuer) in existing:
            print(f"  [{i:2d}/{len(TRUST_LINES)}] {name} — already exists, skipping")
            skip_count += 1
            continue

        tx = build_trust_set_tx(
            wallet.address, currency, issuer, limit, sequence, current_ledger,
        )

        try:
            result = sign_and_submit(tx, wallet)
            engine_result = result.get("engine_result", "unknown")

            if engine_result in ("tesSUCCESS", "terQUEUED"):
                print(f"  [{i:2d}/{len(TRUST_LINES)}] {name} — {engine_result}")
                success_count += 1
                sequence += 1
            else:
                print(f"  [{i:2d}/{len(TRUST_LINES)}] {name} — FAILED: {engine_result}")
                error_msg = result.get("engine_result_message", "")
                if error_msg:
                    print(f"         {error_msg}")
                fail_count += 1
                sequence += 1  # Increment even on failure — sequence is consumed

            # Brief pause between submissions to avoid overwhelming the node
            time.sleep(0.5)

        except Exception as e:
            print(f"  [{i:2d}/{len(TRUST_LINES)}] {name} — ERROR: {e}")
            fail_count += 1

    print()
    print("=" * 50)
    print(f"  Results: {success_count} set, {skip_count} skipped, {fail_count} failed")
    print(f"  Reserve locked: ~{success_count} XRP")
    print("=" * 50)

    if fail_count > 0:
        print("\nSome trust lines failed. Re-run this script to retry — it's idempotent.")


if __name__ == "__main__":
    main()
