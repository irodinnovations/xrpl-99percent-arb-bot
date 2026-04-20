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
    # gross=0.01, fee_ratio=0.000012/1=0.000012, slippage=0.003
    # net = 0.01 - 0.000012 - 0.003 = 0.006988
    assert profit == Decimal("0.01") - Decimal("0.000012") / Decimal("1") - Decimal("0.003")


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


def test_fee_ratio_scales_with_trade_size():
    """Larger trades should have a smaller fee impact (fee is flat 12 drops)."""
    small_trade = calculate_profit(Decimal("0.5"), Decimal("0.505"))
    large_trade = calculate_profit(Decimal("50"), Decimal("50.5"))
    # Same 1% gross profit, but the 12-drop fee matters more on 0.5 XRP
    assert large_trade > small_trade


import importlib
from src.profit_math import get_profit_threshold
from src.config import (
    PROFIT_THRESHOLD_HIGH_LIQ,
    PROFIT_THRESHOLD_LOW_LIQ,
)


def test_get_profit_threshold_high_liq_returns_high_liq():
    assert get_profit_threshold("USD") == PROFIT_THRESHOLD_HIGH_LIQ
    assert get_profit_threshold("USDC") == PROFIT_THRESHOLD_HIGH_LIQ
    assert get_profit_threshold("RLUSD") == PROFIT_THRESHOLD_HIGH_LIQ
    assert get_profit_threshold("EUR") == PROFIT_THRESHOLD_HIGH_LIQ


def test_get_profit_threshold_solo_and_usdt_are_high_liq_after_expansion():
    # Task 2 added these to the default HIGH_LIQ list
    assert get_profit_threshold("SOLO") == PROFIT_THRESHOLD_HIGH_LIQ
    assert get_profit_threshold("USDT") == PROFIT_THRESHOLD_HIGH_LIQ


def test_get_profit_threshold_non_high_liq_returns_low_liq():
    # CLEAN-02: CORE, FUZZY, etc. are in setup_trust_lines.py but NOT in
    # HIGH_LIQ_CURRENCIES — they must fall through to LOW_LIQ, not base.
    assert get_profit_threshold("CORE") == PROFIT_THRESHOLD_LOW_LIQ
    assert get_profit_threshold("FUZZY") == PROFIT_THRESHOLD_LOW_LIQ
    assert get_profit_threshold("UNKNOWN") == PROFIT_THRESHOLD_LOW_LIQ


def test_get_profit_threshold_is_case_insensitive():
    assert get_profit_threshold("usd") == PROFIT_THRESHOLD_HIGH_LIQ
    assert get_profit_threshold("Solo") == PROFIT_THRESHOLD_HIGH_LIQ


def test_get_profit_threshold_returns_decimal_never_float():
    from decimal import Decimal
    result = get_profit_threshold("CORE")
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)


def test_get_profit_threshold_low_liq_env_override(monkeypatch):
    monkeypatch.setenv("PROFIT_THRESHOLD_LOW_LIQ", "0.015")
    import src.config as config_mod
    import src.profit_math as pm_mod
    importlib.reload(config_mod)
    importlib.reload(pm_mod)
    try:
        from decimal import Decimal
        assert pm_mod.get_profit_threshold("CORE") == Decimal("0.015")
    finally:
        monkeypatch.delenv("PROFIT_THRESHOLD_LOW_LIQ", raising=False)
        importlib.reload(config_mod)
        importlib.reload(pm_mod)
