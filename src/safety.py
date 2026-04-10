"""Safety systems — circuit breakers, blacklist, and Decimal enforcement.

SAFE-04: All financial math in this module uses decimal.Decimal — no float.
SAFE-05: Wallet seed is loaded in config.py from .env only — never hardcoded.
DRY-04: DRY_RUN defaults to True in config.py — explicit change required.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from src.config import DAILY_LOSS_LIMIT_PCT

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Return current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


class CircuitBreaker:
    """Daily loss circuit breaker — halts bot for 24h if cumulative loss hits limit.

    SAFE-02: Pauses for 24 hours if daily loss reaches 2% of account balance.
    """

    def __init__(
        self,
        account_address: str,
        connection=None,
        reference_balance: Decimal = Decimal("0"),
        loss_limit_pct: Decimal = DAILY_LOSS_LIMIT_PCT,
    ):
        self.account_address = account_address
        self.connection = connection
        self.reference_balance = reference_balance
        self.loss_limit_pct = loss_limit_pct

        self._daily_pnl: Decimal = Decimal("0")
        self._day_start: datetime = _utcnow()
        self._halt_until: Optional[datetime] = None
        self._trade_count: int = 0

    def _reset_if_new_day(self):
        """Reset daily P&L tracking if 24 hours have passed."""
        now = _utcnow()
        if now - self._day_start >= timedelta(hours=24):
            logger.info(
                f"Daily P&L reset: was {self._daily_pnl} XRP over {self._trade_count} trades"
            )
            self._daily_pnl = Decimal("0")
            self._day_start = now
            self._trade_count = 0

    def is_halted(self) -> bool:
        """Check if circuit breaker is active.

        Returns True if:
        - Currently in 24h halt period, OR
        - Cumulative daily loss exceeds limit
        """
        now = _utcnow()

        # Check if halt period has expired
        if self._halt_until is not None:
            if now >= self._halt_until:
                logger.info("Circuit breaker halt period expired — resuming")
                self._halt_until = None
                self._daily_pnl = Decimal("0")
                self._day_start = now
                self._trade_count = 0
                return False
            return True

        self._reset_if_new_day()
        return False

    def record_trade(self, profit_xrp: Decimal):
        """Record a trade result. Triggers halt if daily loss limit hit.

        profit_xrp: Positive for gains, negative for losses.
        """
        self._reset_if_new_day()
        self._daily_pnl += profit_xrp
        self._trade_count += 1

        logger.info(
            f"Trade recorded: {profit_xrp:+} XRP | "
            f"Daily P&L: {self._daily_pnl} XRP | "
            f"Trades today: {self._trade_count}"
        )

        # Check if loss limit is breached
        if self.reference_balance > Decimal("0"):
            loss_ratio = abs(self._daily_pnl) / self.reference_balance
            if self._daily_pnl < Decimal("0") and loss_ratio >= self.loss_limit_pct:
                self._halt_until = _utcnow() + timedelta(hours=24)
                logger.critical(
                    f"CIRCUIT BREAKER TRIGGERED: Daily loss {loss_ratio * 100:.2f}% "
                    f"exceeds {self.loss_limit_pct * 100:.1f}% limit. "
                    f"Halting for 24 hours until {self._halt_until.isoformat()}"
                )

    async def update_reference_balance(self):
        """Update reference balance from the ledger (call at start of day or on connect)."""
        if self.connection:
            balance = await self.connection.get_account_balance(self.account_address)
            if balance > Decimal("0"):
                self.reference_balance = balance
                logger.info(f"Reference balance updated: {balance} XRP")


class Blacklist:
    """Path and currency blacklist to avoid known-bad or manipulated routes.

    SAFE-03: Prevents trading on known-bad or manipulated routes.
    """

    def __init__(self):
        self._blacklisted_currencies: set[str] = set()
        self._blacklisted_issuers: set[str] = set()

    def add_currency(self, currency: str, issuer: str = ""):
        """Add a currency (and optionally issuer) to the blacklist."""
        self._blacklisted_currencies.add(currency.upper())
        if issuer:
            self._blacklisted_issuers.add(issuer)
        logger.info(f"Blacklisted: {currency}" + (f" issuer {issuer}" if issuer else ""))

    def is_blacklisted(self, paths: list) -> bool:
        """Check if any path step involves a blacklisted currency or issuer.

        paths: List of path arrays from ripple_path_find response.
        Returns True if any blacklisted currency/issuer found.
        """
        if not self._blacklisted_currencies and not self._blacklisted_issuers:
            return False

        for path in paths:
            if isinstance(path, list):
                for step in path:
                    if isinstance(step, dict):
                        currency = step.get("currency", "").upper()
                        issuer = step.get("issuer", "")
                        if currency in self._blacklisted_currencies:
                            logger.warning(f"Blacklisted currency in path: {currency}")
                            return True
                        if issuer and issuer in self._blacklisted_issuers:
                            logger.warning(f"Blacklisted issuer in path: {issuer}")
                            return True

        return False
