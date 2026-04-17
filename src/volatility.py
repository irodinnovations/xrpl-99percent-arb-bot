"""Rolling-window volatility tracker fed by book_changes stream data.

Tracks rate-change magnitudes per currency over a configurable time window
(default 5 minutes).  The volatility factor for a currency is the standard
deviation of its recent rate changes, normalized to 0-1.

This replaces the hardcoded volatility_factor=Decimal("0") throughout the
bot, feeding real market data into slippage calculations and position sizing.

Memory bounded: at ~1 ledger close every 4 seconds and 27 tracked pairs,
a 5-minute window holds at most ~2,025 entries (~50 KB).
"""

import logging
import math
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.config import VOLATILITY_WINDOW

logger = logging.getLogger(__name__)

# Cap volatility at 1.0 to keep slippage calculations bounded.
_MAX_VOLATILITY = Decimal("1")

# Normalization factor: a rate change of 5% maps to volatility=1.0.
# Most XRPL DEX pairs move <0.5% per ledger, so this keeps the
# typical range around 0.01-0.10.
_NORMALIZATION_PCT = Decimal("0.05")


def _is_xrp_side(side: str) -> bool:
    """Return True if the currency side represents XRP.

    Rippled sends "XRP_drops" in the book_changes stream; older docs and
    other RPCs use "XRP". Accept both so we stay robust across versions.
    """
    return side in ("XRP", "XRP_drops")


def _extract_currency_code(side: str) -> Optional[str]:
    """Extract the currency code from an IOU side string.

    Handles both formats we've seen in the wild:
      "ISSUER/CURRENCY"  (current rippled book_changes)
      "CURRENCY/ISSUER"  (older docs)

    A bare rAddress is 25-35 chars base58 — anything else on the split
    is treated as the currency. Returns None if neither side looks like
    a currency.
    """
    if "/" not in side:
        return None
    left, right = side.split("/", 1)
    # An XRPL address starts with 'r' and is 25-35 base58 chars.
    # The other side is the currency (3-char code or 40-char hex).
    if left.startswith("r") and len(left) >= 25:
        return right or None
    return left or None


class VolatilityTracker:
    """Tracks per-currency rate volatility from book_changes stream messages."""

    def __init__(self, window_seconds: int = VOLATILITY_WINDOW):
        self._window = window_seconds
        # Maps currency code -> deque of (timestamp, rate_change_ratio)
        self._changes: dict[str, deque[tuple[float, Decimal]]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        # Diagnostic counters: help verify the book_changes stream
        # is feeding data when global volatility stays at 0.
        self._msgs_processed: int = 0
        self._changes_recorded: int = 0

    def _prune(self, currency: str) -> None:
        """Remove entries older than the rolling window."""
        cutoff = time.time() - self._window
        dq = self._changes.get(currency)
        if not dq:
            return
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def record_change(
        self, currency: str, rate_change_ratio: Decimal
    ) -> None:
        """Record a single rate change observation for a currency.

        Args:
            currency: The currency code (e.g., "USD", "USDC").
            rate_change_ratio: Absolute fractional change (e.g., 0.003 for 0.3%).
        """
        self._changes[currency].append((time.time(), rate_change_ratio))
        self._changes_recorded += 1

    def process_book_changes_message(self, msg: dict) -> None:
        """Parse a book_changes stream message and record rate changes.

        The book_changes message contains a "changes" array where each entry
        has currency pair info and OHLC rates.  We extract the open-to-close
        change magnitude for each affected currency.

        Real XRPL format (verified against wss://s2.ripple.com):
        {
            "type": "bookChanges",
            "ledger_index": 12345,
            "changes": [
                {
                    "currency_a": "XRP_drops",
                    "currency_b": "rIssuerAddr.../CURRENCY_HEX_OR_CODE",
                    "volume_a": "100000000",
                    "volume_b": "50.2",
                    "open": "0.0000005", "close": "...",
                    "high": "...", "low": "..."
                }
            ]
        }

        Note: the XRP sentinel is "XRP_drops" (not "XRP") and the IOU
        format is "ISSUER/CURRENCY" with issuer first. Older docs showed
        the reverse — we accept both to stay robust across rippled versions.
        """
        self._msgs_processed += 1
        changes = msg.get("changes", [])
        if not changes:
            return

        for change in changes:
            try:
                open_rate = change.get("open") or change.get("rate_open")
                close_rate = change.get("close") or change.get("rate_close")
                if not open_rate or not close_rate:
                    continue

                open_dec = Decimal(str(open_rate))
                close_dec = Decimal(str(close_rate))

                if open_dec <= Decimal("0"):
                    continue

                rate_change = abs(close_dec - open_dec) / open_dec

                currency_a = change.get("currency_a", "")
                currency_b = change.get("currency_b", "")

                # Identify the IOU side (the one containing "/").
                # The XRP side will be "XRP" or "XRP_drops".
                if _is_xrp_side(currency_a) and "/" in currency_b:
                    currency = _extract_currency_code(currency_b)
                elif _is_xrp_side(currency_b) and "/" in currency_a:
                    currency = _extract_currency_code(currency_a)
                else:
                    continue

                if currency:
                    self.record_change(currency, rate_change)

            except (InvalidOperation, ArithmeticError, TypeError, ValueError):
                continue

    def get_volatility(self, currency: str) -> Decimal:
        """Get the volatility factor for a currency (0 to 1).

        Computed as the standard deviation of rate changes in the rolling
        window, normalized so that a 5% stddev maps to 1.0.

        Returns Decimal("0") if insufficient data (< 3 observations).
        """
        self._prune(currency)
        dq = self._changes.get(currency)
        if not dq or len(dq) < 3:
            return Decimal("0")

        changes = [float(c[1]) for c in dq]
        try:
            stddev = Decimal(str(math.sqrt(
                sum((x - sum(changes) / len(changes)) ** 2 for x in changes)
                / len(changes)
            )))
        except (ArithmeticError, ValueError):
            return Decimal("0")

        # Normalize: 5% stddev -> 1.0
        normalized = stddev / _NORMALIZATION_PCT
        return min(normalized, _MAX_VOLATILITY)

    def get_changed_currencies(self, since: float) -> set[str]:
        """Return currency codes that recorded rate changes since a timestamp.

        Used by the event-driven scanner to identify which pairs changed
        in the latest ledger and need targeted re-scanning.

        Args:
            since: Unix timestamp. Returns currencies with any change after this.
        """
        changed = set()
        for currency, dq in self._changes.items():
            if dq and dq[-1][0] >= since:
                changed.add(currency)
        return changed

    def get_diagnostics(self) -> dict:
        """Return counters for debugging whether the tracker is receiving data.

        Used by the heartbeat log so operators can tell the difference
        between 'no volatility because markets are quiet' and 'no volatility
        because the book_changes stream isn't wired up'.
        """
        return {
            "msgs_processed": self._msgs_processed,
            "changes_recorded": self._changes_recorded,
            "currencies_tracked": len(self._changes),
        }

    def get_global_volatility(self) -> Decimal:
        """Average volatility across all tracked currencies.

        Useful as a fallback when pair-specific data is sparse.
        """
        if not self._changes:
            return Decimal("0")

        vols = []
        for currency in list(self._changes.keys()):
            vol = self.get_volatility(currency)
            if vol > Decimal("0"):
                vols.append(vol)

        if not vols:
            return Decimal("0")

        return sum(vols) / Decimal(str(len(vols)))
