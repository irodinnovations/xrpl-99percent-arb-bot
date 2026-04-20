"""Pure Decimal profit calculation — no floats allowed anywhere."""

from decimal import Decimal
from typing import Optional

from src.config import (
    PROFIT_THRESHOLD,
    PROFIT_THRESHOLD_HIGH_LIQ,
    PROFIT_THRESHOLD_LOW_LIQ,
    HIGH_LIQ_CURRENCIES,
    SLIPPAGE_BASE,
    NETWORK_FEE,
    MAX_POSITION_PCT,
    MIN_POSITION_PCT,
)


def calculate_slippage(volatility_factor: Decimal = Decimal("0")) -> Decimal:
    """Calculate slippage buffer: base + (0.001 * volatility_factor).

    volatility_factor: 0-1 Decimal representing 5-minute volatility.
    Returns: Slippage buffer as Decimal.
    """
    dynamic_component = Decimal("0.001") * volatility_factor
    return SLIPPAGE_BASE + dynamic_component


def calculate_profit(
    input_xrp: Decimal,
    output_xrp: Decimal,
    volatility_factor: Decimal = Decimal("0"),
) -> Decimal:
    """Calculate net profit ratio after fees and slippage.

    Formula: ((output - input) / input) - (network_fee / input) - slippage_buffer

    NETWORK_FEE is a flat cost in XRP (12 drops = 0.000012 XRP), so we convert
    it to a ratio relative to the actual trade size. On a 0.5 XRP trade this is
    0.0024%; on a 50 XRP trade it's 0.000024%.

    Returns: Net profit as a Decimal ratio (e.g., 0.008 = 0.8%).
    """
    gross_ratio = (output_xrp - input_xrp) / input_xrp
    fee_ratio = NETWORK_FEE / input_xrp
    slippage = calculate_slippage(volatility_factor)
    return gross_ratio - fee_ratio - slippage


def is_profitable(
    input_xrp: Decimal,
    output_xrp: Decimal,
    volatility_factor: Decimal = Decimal("0"),
    threshold: Optional[Decimal] = None,
) -> bool:
    """Check if a trade exceeds the profit threshold (strictly greater than).

    Args:
        threshold: Override the default PROFIT_THRESHOLD.  Use with
                   get_profit_threshold() for tiered thresholds.
    """
    profit = calculate_profit(input_xrp, output_xrp, volatility_factor)
    effective = threshold if threshold is not None else PROFIT_THRESHOLD
    return profit > effective


def calculate_position_size(account_balance: Decimal) -> Decimal:
    """Calculate max trade size as MAX_POSITION_PCT of account balance.

    Returns: Maximum XRP to risk on a single trade.
    """
    return account_balance * MAX_POSITION_PCT


def get_profit_threshold(currency: str) -> Decimal:
    """Return the profit threshold appropriate for a currency's liquidity class.

    Three-tier model:
      1. HIGH_LIQ (explicit list) → PROFIT_THRESHOLD_HIGH_LIQ (deep books, low slippage)
      2. everything else → PROFIT_THRESHOLD_LOW_LIQ (thinner books, higher risk)

    The base PROFIT_THRESHOLD stays as the default for PROFIT_THRESHOLD_LOW_LIQ
    when unset (src/config.py env default 0.010), so a user can still fall
    through to a single-threshold model by setting both HIGH_LIQ and LOW_LIQ
    to the same value.
    """
    if currency.upper() in [c.strip().upper() for c in HIGH_LIQ_CURRENCIES]:
        return PROFIT_THRESHOLD_HIGH_LIQ
    return PROFIT_THRESHOLD_LOW_LIQ


def calculate_dynamic_position(
    account_balance: Decimal,
    profit_ratio: Decimal,
    volatility_factor: Decimal = Decimal("0"),
) -> Decimal:
    """Calculate position size scaled by opportunity quality and volatility.

    Higher profit ratio -> larger position (more confident opportunity).
    Higher volatility -> smaller position (more slippage risk).

    Position is clamped between MIN_POSITION_PCT and MAX_POSITION_PCT of
    the account balance.

    Args:
        account_balance: Current XRP balance.
        profit_ratio: Expected profit as a ratio (e.g., 0.008 = 0.8%).
        volatility_factor: 0-1 Decimal from VolatilityTracker.

    Returns:
        Position size in XRP.
    """
    # Quality signal: 0.6% profit -> min position, 2%+ profit -> max position
    quality = min(profit_ratio / Decimal("0.02"), Decimal("1"))

    # Volatility penalty: high volatility reduces position size
    vol_penalty = Decimal("1") - (volatility_factor * Decimal("0.5"))
    vol_penalty = max(vol_penalty, Decimal("0.3"))  # Never reduce below 30%

    pct = MIN_POSITION_PCT + (MAX_POSITION_PCT - MIN_POSITION_PCT) * quality * vol_penalty
    pct = max(MIN_POSITION_PCT, min(pct, MAX_POSITION_PCT))
    return account_balance * pct
