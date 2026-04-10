"""Tests for circuit breaker and blacklist safety systems."""

import pytest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import patch
from src.safety import CircuitBreaker, Blacklist


class TestCircuitBreaker:
    def test_not_halted_initially(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        assert cb.is_halted() is False

    def test_halted_after_loss_limit(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        # 2% of 100 = 2 XRP loss triggers halt
        cb.record_trade(Decimal("-2"))
        assert cb.is_halted() is True

    def test_not_halted_with_small_loss(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("-1"))  # 1% loss, under 2% limit
        assert cb.is_halted() is False

    def test_accumulates_losses(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("-1"))
        cb.record_trade(Decimal("-1"))  # Now at -2 XRP = 2% of 100
        assert cb.is_halted() is True

    def test_gains_offset_losses(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("-1.5"))
        cb.record_trade(Decimal("1"))  # Net = -0.5 XRP
        assert cb.is_halted() is False

    def test_halt_expires_after_24h(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("-2"))  # Triggers halt
        assert cb.is_halted() is True

        # Simulate 24 hours passing
        cb._halt_until = datetime.utcnow() - timedelta(hours=1)
        assert cb.is_halted() is False

    def test_daily_pnl_is_decimal(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        cb.record_trade(Decimal("0.5"))
        assert isinstance(cb._daily_pnl, Decimal)

    def test_reference_balance_is_decimal(self):
        cb = CircuitBreaker(account_address="rTest", reference_balance=Decimal("100"))
        assert isinstance(cb.reference_balance, Decimal)


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
