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

# Trading — all financial values as Decimal.
#
# Two-leg rewrite (Apr 2026) raised the default profit threshold from
# 0.6% to 1.0% and dropped default position from 5% to 2%. The new
# defaults price in the non-atomic risk of two sequential Payments; if
# your .env overrides these, keep them at/above the new defaults.
DRY_RUN: bool = os.getenv("DRY_RUN", "True").lower() in ("true", "1", "yes")
PROFIT_THRESHOLD: Decimal = Decimal(os.getenv("PROFIT_THRESHOLD", "0.010"))
MAX_POSITION_PCT: Decimal = Decimal(os.getenv("MAX_POSITION_PCT", "0.02"))
# Multi-tier scanning: probe at multiple position sizes per ledger cycle
# to surface opportunities at different trade amounts (1%, 2%, 5% of balance).
POSITION_TIERS: list[Decimal] = [Decimal("0.01"), Decimal("0.02"), Decimal("0.05")]
DAILY_LOSS_LIMIT_PCT: Decimal = Decimal(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.01"))
SLIPPAGE_BASE: Decimal = Decimal(os.getenv("SLIPPAGE_BASE", "0.003"))
NETWORK_FEE: Decimal = Decimal("0.000012")  # ~12 drops, standard XRPL fee

# --- Two-leg execution tuning (see docs/two_leg_architecture.md) ---

# Recovery flow: retry leg 2 up to this many times with a fresh LLS.
LEG2_RETRY_MAX: int = int(os.getenv("LEG2_RETRY_MAX", "2"))

# Max allowable spread drift on leg 2 retry before bailing to market-dump.
LEG2_RETRY_SPREAD_TOLERANCE: Decimal = Decimal(
    os.getenv("LEG2_RETRY_SPREAD_TOLERANCE", "0.003")
)

# LastLedgerSequence window (in ledgers) for each leg's validation deadline.
LEG2_TIMEOUT_LEDGERS: int = int(os.getenv("LEG2_TIMEOUT_LEDGERS", "4"))

# Maximum loss accepted during emergency IOU market-dump recovery.
RECOVERY_MAX_LOSS_PCT: Decimal = Decimal(
    os.getenv("RECOVERY_MAX_LOSS_PCT", "0.02")
)

# --- Autonomous safety rails ---

# Absolute XRP cap per trade regardless of MAX_POSITION_PCT. Final line of
# defense against balance-calculation bugs — no trade ever exceeds this.
MAX_TRADE_XRP_ABS: Decimal = Decimal(os.getenv("MAX_TRADE_XRP_ABS", "5.0"))

# Skip all trades if current balance falls below this fraction of the
# reference balance. Defense against slow drain or balance corruption.
MIN_BALANCE_GUARD_PCT: Decimal = Decimal(
    os.getenv("MIN_BALANCE_GUARD_PCT", "0.95")
)

# Cooldown after 3 consecutive mid-trade recovery failures.
MID_TRADE_HALT_HOURS: int = int(os.getenv("MID_TRADE_HALT_HOURS", "2"))

# TTL for blacklisted routes — auto-expiry after this duration.
ROUTE_BLACKLIST_HOURS: int = int(os.getenv("ROUTE_BLACKLIST_HOURS", "24"))

# Consecutive sim failures on the same route before blacklisting.
SIM_FAIL_BLACKLIST_COUNT: int = int(os.getenv("SIM_FAIL_BLACKLIST_COUNT", "3"))

# Sliding window for counting sim failures (seconds).
SIM_FAIL_WINDOW_SECONDS: int = int(os.getenv("SIM_FAIL_WINDOW_SECONDS", "3600"))

# --- Post-probation scaling ---

# Number of consecutive clean days before position cap relaxes.
PROBATION_DAYS: int = int(os.getenv("PROBATION_DAYS", "7"))

# Maximum position after successful probation completion.
POST_PROBATION_MAX_POSITION_PCT: Decimal = Decimal(
    os.getenv("POST_PROBATION_MAX_POSITION_PCT", "0.05")
)

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
