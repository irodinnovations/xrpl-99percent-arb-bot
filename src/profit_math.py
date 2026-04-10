"""Pure Decimal profit calculation — no floats allowed anywhere."""

from decimal import Decimal
from src.config import PROFIT_THRESHOLD, SLIPPAGE_BASE, NETWORK_FEE, MAX_POSITION_PCT


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

    Formula: ((output - input) / input) - network_fee - slippage_buffer
    Returns: Net profit as a Decimal ratio (e.g., 0.008 = 0.8%).
    """
    gross_ratio = (output_xrp - input_xrp) / input_xrp
    slippage = calculate_slippage(volatility_factor)
    return gross_ratio - NETWORK_FEE - slippage


def is_profitable(
    input_xrp: Decimal,
    output_xrp: Decimal,
    volatility_factor: Decimal = Decimal("0"),
) -> bool:
    """Check if a trade exceeds the profit threshold (strictly greater than)."""
    profit = calculate_profit(input_xrp, output_xrp, volatility_factor)
    return profit > PROFIT_THRESHOLD


def calculate_position_size(account_balance: Decimal) -> Decimal:
    """Calculate max trade size as MAX_POSITION_PCT of account balance.

    Returns: Maximum XRP to risk on a single trade.
    """
    return account_balance * MAX_POSITION_PCT
