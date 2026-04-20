"""JSONL trade logger and console logging setup.

LOG-01: All trades logged to xrpl_arb_log.jsonl in append-only JSON Lines format.
LOG-02: Each entry includes timestamp, profit_pct, input_xrp, simulated_output, dry_run, hash.
LOG-03: Console logging uses Python standard logging with timestamps and levels.
LOG-04: Log file is shared between bot and Streamlit dashboard.
DRY-03: Paper trades logged identically to live trades (with dry_run: true flag).
"""

import json
import logging
from datetime import datetime, timezone

from src.config import LOG_FILE, LOG_LEVEL

logger = logging.getLogger(__name__)


def setup_logging():
    """Configure console logging with timestamps and levels (LOG-03).

    Format: 2026-04-10 12:34:56,789 [INFO] Message here

    Explicitly sets root logger level to override any prior configuration
    (e.g., pytest's default WARNING level).
    """
    log_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Force level on root logger even if basicConfig was a no-op due to existing handlers
    logging.getLogger().setLevel(log_level)
    # Reduce noise from third-party libraries
    logging.getLogger("xrpl").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def log_trade(data: dict) -> None:
    """Append a trade entry to the JSONL log file (LOG-01, LOG-02).

    Adds UTC timestamp to every entry. All fields from data dict are preserved.
    Paper and live trades are logged identically — differentiated by dry_run flag (DRY-03).

    Required fields in data (enforced by caller):
    - profit_pct: str (Decimal string)
    - input_xrp: str (Decimal string)
    - simulated_output: str (Decimal string)
    - dry_run: bool
    - hash: str (only for live trades)
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        logger.debug(f"Trade logged to {LOG_FILE}")
    except OSError as e:
        logger.error(f"Failed to write trade log: {e}")


async def log_trade_leg(
    *,
    leg: int,
    sequence: int,
    hash: str,
    engine_result: str,
    ledger_index: int,
    dry_run: bool,
    latency_from_leg1_ms: int | None = None,
    path_used: list | None = None,
    extra: dict | None = None,
) -> None:
    """Append a per-leg JSONL entry for atomic two-leg submission (ATOM-09).

    Schema is ADDITIVE vs log_trade — readers (dashboard, backtester) that look up
    specific keys continue to work; they only need to filter by `entry_type == "leg"`
    if they want to ignore per-leg rows.

    The `path_used` field (added per plan-checker Warning 5) captures the actual
    Paths array for the submitted leg so post-incident analysis can distinguish
    "atomic submit wasn't the fix" from "path splitting needed" if leg 2 ever
    fails tecPATH_PARTIAL despite the atomic window.

    Required positional-only fields enforced by keyword-only signature so future
    additions don't accidentally shift positions.
    """
    entry = {
        "entry_type": "leg",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "leg": leg,
        "sequence": sequence,
        "hash": hash,
        "engine_result": engine_result,
        "ledger_index": ledger_index,
        "dry_run": dry_run,
    }
    if latency_from_leg1_ms is not None:
        entry["latency_from_leg1_ms"] = int(latency_from_leg1_ms)
    if path_used is not None:
        entry["path_used"] = path_used
    if extra:
        entry.update(extra)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        logger.error(f"Failed to write leg log: {e}")


async def log_trade_summary(
    *,
    outcome: str,
    dry_run: bool,
    profit_pct: "Decimal | str | None" = None,
    net_profit_xrp: "Decimal | str | None" = None,
    leg1_hash: str | None = None,
    leg2_hash: str | None = None,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append a trade-level summary entry aggregating both legs.

    `outcome` values used by the atomic executor:
      - "both_legs_success"
      - "leg1_fail_burned"            (leg 1 tec/tef/tem; leg 2 Sequence burned via AccountSet)
      - "leg1_fail_burn_failed"       (leg 1 failed AND the burn also failed — escalated alert)
      - "leg2_fail_recovery_activated"(leg 1 committed, leg 2 failed, 2% recovery hit)
      - "dry_run_would_execute"       (DRY_RUN=True, would-execute log)
      - "pre_submit_gate_failed"      (sim1 or sim2 rejected before submit)
    """
    entry = {
        "entry_type": "summary",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "dry_run": dry_run,
    }
    if profit_pct is not None:
        entry["profit_pct"] = str(profit_pct)
    if net_profit_xrp is not None:
        entry["net_profit_xrp"] = str(net_profit_xrp)
    if leg1_hash is not None:
        entry["leg1_hash"] = leg1_hash
    if leg2_hash is not None:
        entry["leg2_hash"] = leg2_hash
    if error is not None:
        entry["error"] = error
    if extra:
        entry.update(extra)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        logger.error(f"Failed to write summary log: {e}")
