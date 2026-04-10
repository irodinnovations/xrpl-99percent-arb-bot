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
