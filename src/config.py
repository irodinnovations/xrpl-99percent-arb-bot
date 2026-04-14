"""Centralized configuration loaded from .env with Decimal constants."""

import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

# XRPL Connection
XRPL_SECRET: str = os.getenv("XRPL_SECRET", "")

_ws_url = os.getenv("XRPL_WS_URL", "wss://s1.ripple.com")
if not _ws_url.startswith("wss://"):
    raise ValueError(
        f"XRPL_WS_URL must use wss:// (TLS) — got: {_ws_url!r}. "
        "Plain ws:// connections are not allowed (threat T-01-01)."
    )
XRPL_WS_URL: str = _ws_url

XRPL_RPC_URL: str = os.getenv("XRPL_RPC_URL", "https://s1.ripple.com")

# Trading — all financial values as Decimal
DRY_RUN: bool = os.getenv("DRY_RUN", "True").lower() in ("true", "1", "yes")
PROFIT_THRESHOLD: Decimal = Decimal(os.getenv("PROFIT_THRESHOLD", "0.006"))
MAX_POSITION_PCT: Decimal = Decimal(os.getenv("MAX_POSITION_PCT", "0.05"))
# Multi-tier scanning: probe at multiple position sizes per ledger cycle
# to surface opportunities at different trade amounts (1%, 5%, 10% of balance).
POSITION_TIERS: list[Decimal] = [Decimal("0.01"), Decimal("0.05"), Decimal("0.10")]
DAILY_LOSS_LIMIT_PCT: Decimal = Decimal(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.02"))
SLIPPAGE_BASE: Decimal = Decimal(os.getenv("SLIPPAGE_BASE", "0.003"))
NETWORK_FEE: Decimal = Decimal("0.000012")  # ~12 drops, standard XRPL fee

# Scan interval: run FULL pathfinder every N ledger closes (~4-7s each).
# The full scan is now a periodic fallback — event-driven scan_pairs()
# handles the hot path every ledger close via book_changes stream.
# Full scan includes multi-hop discovery via ripple_path_find which is
# heavier, so we run it less frequently.  Default 8 means one full
# scan every ~30-55 seconds.
SCAN_INTERVAL: int = int(os.getenv("SCAN_INTERVAL", "8"))

# Tiered profit thresholds by liquidity class
PROFIT_THRESHOLD_HIGH_LIQ: Decimal = Decimal(
    os.getenv("PROFIT_THRESHOLD_HIGH_LIQ", "0.003")
)
PROFIT_THRESHOLD_LOW_LIQ: Decimal = Decimal(
    os.getenv("PROFIT_THRESHOLD_LOW_LIQ", "0.010")
)
HIGH_LIQ_CURRENCIES: list[str] = os.getenv(
    "HIGH_LIQ_CURRENCIES", "USD,USDC,RLUSD,EUR"
).split(",")

# Dynamic position sizing range (MIN to MAX_POSITION_PCT)
MIN_POSITION_PCT: Decimal = Decimal(os.getenv("MIN_POSITION_PCT", "0.01"))

# Volatility tracking window (seconds)
VOLATILITY_WINDOW: int = int(os.getenv("VOLATILITY_WINDOW", "300"))

# AMM event detection minimum impact (XRP)
AMM_MIN_IMPACT_XRP: Decimal = Decimal(os.getenv("AMM_MIN_IMPACT_XRP", "10"))

# Telegram (optional)
TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# AI Brain (optional — AI-04)
ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_KEY", "")
AI_REVIEWS_FILE: str = os.getenv("AI_REVIEWS_FILE", "ai_reviews.jsonl")

# Logging
LOG_FILE: str = os.getenv("LOG_FILE", "xrpl_arb_log.jsonl")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
