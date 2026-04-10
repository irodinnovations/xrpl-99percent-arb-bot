"""Tests for profit math — every assertion checks Decimal type."""

import pytest
from decimal import Decimal
from src.profit_math import (
    calculate_profit,
    calculate_slippage,
    is_profitable,
    calculate_position_size,
)


def test_calculate_profit_basic():
    profit = calculate_profit(Decimal("1"), Decimal("1.01"))
    assert isinstance(profit, Decimal)
    # 0.01 - 0.000012 - 0.003 = 0.006988
    assert profit == Decimal("1.01") / Decimal("1") - 1 - Decimal("0.000012") - Decimal("0.003")


def test_calculate_profit_returns_decimal():
    result = calculate_profit(Decimal("10"), Decimal("10.1"))
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)


def test_is_profitable_above_threshold():
    # 2% gross profit should be well above 0.6% threshold
    assert is_profitable(Decimal("1"), Decimal("1.02")) is True


def test_is_profitable_below_threshold():
    # 0.1% gross profit should be below threshold
    assert is_profitable(Decimal("1"), Decimal("1.001")) is False


def test_is_profitable_at_exact_threshold_returns_false():
    # Profit must EXCEED threshold, not equal it
    # We need output such that profit == exactly 0.006
    # profit = (out - 1) / 1 - 0.000012 - 0.003 = 0.006
    # (out - 1) = 0.009012 => out = 1.009012
    assert is_profitable(Decimal("1"), Decimal("1.009012")) is False


def test_calculate_slippage_zero_volatility():
    slip = calculate_slippage(Decimal("0"))
    assert slip == Decimal("0.003")
    assert isinstance(slip, Decimal)


def test_calculate_slippage_with_volatility():
    slip = calculate_slippage(Decimal("0.5"))
    assert slip == Decimal("0.0035")
    assert isinstance(slip, Decimal)


def test_position_size_basic():
    size = calculate_position_size(Decimal("100"))
    assert size == Decimal("5")  # 5% of 100
    assert isinstance(size, Decimal)


def test_position_size_never_exceeds_five_percent():
    size = calculate_position_size(Decimal("1000"))
    assert size == Decimal("50")  # exactly 5%
    assert size <= Decimal("1000") * Decimal("0.05")
