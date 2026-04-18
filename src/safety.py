"""Safety systems — circuit breakers, blacklist, and Decimal enforcement.

SAFE-04: All financial math in this module uses decimal.Decimal — no float.
SAFE-05: Wallet seed is loaded in config.py from .env only — never hardcoded.
DRY-04: DRY_RUN defaults to True in config.py — explicit change required.

B5 two-leg additions
--------------------
- Route-keyed blacklist entries with TTL-based auto-expiry so the bot
  can block broken routes without human intervention.
- Sim-failure sliding-window counter: N failures within a time window
  on the same route auto-triggers a route block.
- Legacy per-currency/issuer blacklist kept for main.py compatibility.
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from src.config import (
    DAILY_LOSS_LIMIT_PCT,
    ROUTE_BLACKLIST_HOURS,
    SIM_FAIL_BLACKLIST_COUNT,
    SIM_FAIL_WINDOW_SECONDS,
)

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

    def halt_for(self, hours: int, reason: str = "") -> None:
        """Trigger a manual time-boxed halt independent of daily P&L.

        Used by the recovery flow when mid-trade dumps fail repeatedly
        and we need to pause all trading for MID_TRADE_HALT_HOURS. The
        halt auto-expires like any other — no human action required.
        """
        self._halt_until = _utcnow() + timedelta(hours=hours)
        logger.critical(
            f"CIRCUIT BREAKER MANUAL HALT for {hours}h until "
            f"{self._halt_until.isoformat()} — reason: {reason or 'unspecified'}"
        )


class Blacklist:
    """Route and currency blacklist with time-expiring entries.

    Three block layers coexist:
      1. Permanent currency/issuer blocklist (add_currency) — checked by
         is_blacklisted(paths). Used by main.py for known-bad issuers.
      2. Route-keyed time-expiring block (block_route + is_route_blocked).
         Auto-clears after ROUTE_BLACKLIST_HOURS. Fed by recovery flow
         and sim-failure counter.
      3. Sliding-window sim-failure counter (record_sim_failure). N fails
         within SIM_FAIL_WINDOW_SECONDS auto-triggers a route block.

    SAFE-03: Prevents trading on known-bad or manipulated routes.
    """

    def __init__(
        self,
        route_ttl_hours: int = ROUTE_BLACKLIST_HOURS,
        sim_fail_threshold: int = SIM_FAIL_BLACKLIST_COUNT,
        sim_fail_window_seconds: int = SIM_FAIL_WINDOW_SECONDS,
    ):
        # Legacy permanent blocklist
        self._blacklisted_currencies: set[str] = set()
        self._blacklisted_issuers: set[str] = set()
        # Route-keyed TTL blocks (new in B5)
        self._route_expiry: dict[str, datetime] = {}
        self._route_ttl_hours = route_ttl_hours
        # Sliding-window sim failure timestamps per route
        self._sim_failures: dict[str, deque] = defaultdict(deque)
        self._sim_fail_threshold = sim_fail_threshold
        self._sim_fail_window_seconds = sim_fail_window_seconds

    # ------------------------------------------------------------------
    # Permanent currency/issuer blacklist (legacy — used by main.py)
    # ------------------------------------------------------------------

    def add_currency(self, currency: str, issuer: str = ""):
        """Add a currency (and optionally issuer) to the permanent blacklist."""
        self._blacklisted_currencies.add(currency.upper())
        if issuer:
            self._blacklisted_issuers.add(issuer)
        logger.info(f"Blacklisted: {currency}" + (f" issuer {issuer}" if issuer else ""))

    def is_blacklisted(self, paths: list) -> bool:
        """Check if any path step involves a blacklisted currency or issuer.

        paths: list of path arrays from pathfinder.  Returns True on any
        match against the permanent currency/issuer blocklist.
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

    # ------------------------------------------------------------------
    # Route-keyed time-expiring blocks (B5)
    # ------------------------------------------------------------------

    def block_route(self, route_key: str, hours: Optional[int] = None) -> None:
        """Block a route for `hours` (default: ROUTE_BLACKLIST_HOURS).

        Idempotent — re-blocking an active route extends the expiry.
        """
        ttl = hours if hours is not None else self._route_ttl_hours
        expiry = _utcnow() + timedelta(hours=ttl)
        self._route_expiry[route_key] = expiry
        logger.warning(
            f"Route blacklisted for {ttl}h until {expiry.isoformat()}: {route_key}"
        )

    def is_route_blocked(self, route_key: str) -> bool:
        """True if the route is currently blocked. Auto-purges expired entries."""
        self._purge_expired_routes()
        return route_key in self._route_expiry

    def record_sim_failure(self, route_key: str) -> bool:
        """Record one sim failure on `route_key`.

        If the route hits `sim_fail_threshold` failures within
        `sim_fail_window_seconds`, the route is automatically blocked
        and the counter is cleared. Returns True iff that auto-block
        fired on this call.
        """
        now = _utcnow()
        cutoff = now - timedelta(seconds=self._sim_fail_window_seconds)
        failures = self._sim_failures[route_key]

        while failures and failures[0] < cutoff:
            failures.popleft()

        failures.append(now)
        if len(failures) >= self._sim_fail_threshold:
            logger.critical(
                f"Route hit {len(failures)} sim failures within "
                f"{self._sim_fail_window_seconds}s — auto-blocking"
            )
            self.block_route(route_key)
            failures.clear()
            return True
        return False

    def _purge_expired_routes(self) -> None:
        now = _utcnow()
        expired = [k for k, exp in self._route_expiry.items() if now >= exp]
        for k in expired:
            del self._route_expiry[k]
            logger.info(f"Route blacklist expired, re-allowed: {k}")
