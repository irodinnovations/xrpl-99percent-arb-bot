"""Tests for circuit breaker and blacklist safety systems."""

import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from src.safety import CircuitBreaker, Blacklist


class TestCircuitBreaker:
    # These tests pin loss_limit_pct to 0.02 explicitly so they remain
    # stable regardless of the config default (which the two-leg rewrite
    # tightened from 2% to 1%). Other tests exercise the default limit.
    _LIMIT = Decimal("0.02")

    def _cb(self):
        return CircuitBreaker(
            account_address="rTest",
            reference_balance=Decimal("100"),
            loss_limit_pct=self._LIMIT,
        )

    def test_not_halted_initially(self):
        assert self._cb().is_halted() is False

    def test_halted_after_loss_limit(self):
        cb = self._cb()
        # 2% of 100 = 2 XRP loss triggers halt
        cb.record_trade(Decimal("-2"))
        assert cb.is_halted() is True

    def test_not_halted_with_small_loss(self):
        cb = self._cb()
        cb.record_trade(Decimal("-1"))  # 1% loss, under 2% limit
        assert cb.is_halted() is False

    def test_accumulates_losses(self):
        cb = self._cb()
        cb.record_trade(Decimal("-1"))
        cb.record_trade(Decimal("-1"))  # Now at -2 XRP = 2% of 100
        assert cb.is_halted() is True

    def test_gains_offset_losses(self):
        cb = self._cb()
        cb.record_trade(Decimal("-1.5"))
        cb.record_trade(Decimal("1"))  # Net = -0.5 XRP
        assert cb.is_halted() is False

    def test_halt_expires_after_24h(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("-2"))  # Triggers halt
        assert cb.is_halted() is True

        # Simulate 24 hours passing
        cb._halt_until = datetime.now(timezone.utc) - timedelta(hours=1)
        assert cb.is_halted() is False

    def test_daily_pnl_is_decimal(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("0.5"))
        assert isinstance(cb._daily_pnl, Decimal)

    def test_reference_balance_is_decimal(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        assert isinstance(cb.reference_balance, Decimal)

    def test_halt_for_sets_halt_until(self):
        """halt_for triggers a manual time-boxed halt regardless of P&L."""
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        assert cb.is_halted() is False
        cb.halt_for(hours=2, reason="mid_trade_recovery_failed")
        assert cb.is_halted() is True
        # Manual halt also auto-expires like daily-loss halt
        cb._halt_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert cb.is_halted() is False


class TestBlacklist:
    def test_empty_blacklist_allows_all(self):
        bl = Blacklist()
        paths = [[{"currency": "USD", "issuer": "rIssuer"}]]
        assert bl.is_blacklisted(paths) is False

    def test_blacklisted_currency_detected(self):
        bl = Blacklist()
        bl.add_currency("SCAM")
        paths = [[{"currency": "SCAM", "issuer": "rBadIssuer"}]]
        assert bl.is_blacklisted(paths) is True

    def test_clean_paths_pass(self):
        bl = Blacklist()
        bl.add_currency("SCAM")
        paths = [[{"currency": "USD", "issuer": "rGoodIssuer"}]]
        assert bl.is_blacklisted(paths) is False

    def test_add_currency_case_insensitive(self):
        bl = Blacklist()
        bl.add_currency("scam")
        paths = [[{"currency": "SCAM", "issuer": "rBadIssuer"}]]
        assert bl.is_blacklisted(paths) is True

    def test_blacklisted_issuer_detected(self):
        bl = Blacklist()
        bl.add_currency("USD", issuer="rBadIssuer")
        paths = [[{"currency": "EUR", "issuer": "rBadIssuer"}]]
        assert bl.is_blacklisted(paths) is True

    def test_empty_paths_not_blacklisted(self):
        bl = Blacklist()
        bl.add_currency("SCAM")
        assert bl.is_blacklisted([]) is False


class TestBlacklistRouteBlocking:
    """Route-keyed time-expiring blocks added in Phase B5."""

    def test_route_not_blocked_by_default(self):
        bl = Blacklist()
        assert bl.is_route_blocked("USD|rBuy|rSell") is False

    def test_block_route_sets_entry(self):
        bl = Blacklist()
        bl.block_route("USD|rBuy|rSell", hours=24)
        assert bl.is_route_blocked("USD|rBuy|rSell") is True

    def test_different_route_unaffected(self):
        bl = Blacklist()
        bl.block_route("USD|rBuy|rSell", hours=24)
        assert bl.is_route_blocked("EUR|rOther|rOther") is False

    def test_block_route_auto_expires(self):
        bl = Blacklist()
        bl.block_route("USD|rBuy|rSell", hours=24)
        # Simulate expiry by backdating the stored timestamp
        bl._route_expiry["USD|rBuy|rSell"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        # First call triggers purge and auto-clears expired entry
        assert bl.is_route_blocked("USD|rBuy|rSell") is False
        assert "USD|rBuy|rSell" not in bl._route_expiry

    def test_block_route_reblock_extends(self):
        bl = Blacklist()
        bl.block_route("USD|rBuy|rSell", hours=1)
        first_expiry = bl._route_expiry["USD|rBuy|rSell"]
        bl.block_route("USD|rBuy|rSell", hours=24)
        second_expiry = bl._route_expiry["USD|rBuy|rSell"]
        assert second_expiry > first_expiry


class TestBlacklistSimFailureCounter:
    """Sliding-window auto-blocklist: N sim fails → route blocked."""

    def test_first_failure_does_not_block(self):
        bl = Blacklist(sim_fail_threshold=3, sim_fail_window_seconds=3600)
        triggered = bl.record_sim_failure("USD|rBuy|rSell")
        assert triggered is False
        assert bl.is_route_blocked("USD|rBuy|rSell") is False

    def test_threshold_reached_auto_blocks(self):
        bl = Blacklist(sim_fail_threshold=3, sim_fail_window_seconds=3600)
        bl.record_sim_failure("USD|rBuy|rSell")
        bl.record_sim_failure("USD|rBuy|rSell")
        triggered = bl.record_sim_failure("USD|rBuy|rSell")
        assert triggered is True
        assert bl.is_route_blocked("USD|rBuy|rSell") is True

    def test_block_clears_counter(self):
        bl = Blacklist(sim_fail_threshold=3, sim_fail_window_seconds=3600)
        for _ in range(3):
            bl.record_sim_failure("USD|rBuy|rSell")
        # Counter is cleared after triggering a block
        assert len(bl._sim_failures["USD|rBuy|rSell"]) == 0

    def test_old_failures_outside_window_pruned(self):
        bl = Blacklist(sim_fail_threshold=3, sim_fail_window_seconds=60)
        # Two failures 10 minutes ago → outside the 60s window
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        bl._sim_failures["USD|rBuy|rSell"].append(old_ts)
        bl._sim_failures["USD|rBuy|rSell"].append(old_ts)
        # New failure should NOT trigger — old ones get pruned
        triggered = bl.record_sim_failure("USD|rBuy|rSell")
        assert triggered is False
        assert bl.is_route_blocked("USD|rBuy|rSell") is False

    def test_different_routes_counted_separately(self):
        bl = Blacklist(sim_fail_threshold=3, sim_fail_window_seconds=3600)
        for _ in range(2):
            bl.record_sim_failure("USD|rA|rB")
            bl.record_sim_failure("EUR|rC|rD")
        # Each route has 2 failures — neither at threshold
        assert bl.is_route_blocked("USD|rA|rB") is False
        assert bl.is_route_blocked("EUR|rC|rD") is False
