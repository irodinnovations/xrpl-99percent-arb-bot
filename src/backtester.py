"""Backtesting engine that parses JSONL logs and computes trade performance metrics.

BACK-01: Parses xrpl_arb_log.jsonl and replays logged results without re-simulating.
BACK-02: Computes win rate, total opportunities, avg profit, max profit, max loss,
         and profit distribution using Decimal math.

All monetary calculations use decimal.Decimal — no floats.

# Future enhancement: Add optional historical ledger replay mode using XRPL API
# for broader testing beyond just paper trading logs.
"""

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)


class BacktestEngine:
    """Loads paper trading logs from a JSONL file for backtesting analysis (BACK-01).

    Parses each line as a JSON trade record. Malformed lines are skipped with a
    warning log — the engine never crashes on bad input (mitigates threat T-02-01).
    """

    def __init__(self, log_file: str, last_n: Optional[int] = None) -> None:
        """Initialize the backtesting engine.

        Args:
            log_file: Path to the JSONL log file (xrpl_arb_log.jsonl).
            last_n: If set, return only the last N trade entries.
        """
        self.log_file = log_file
        self.last_n = last_n

    def load_trades(self) -> list[dict]:
        """Read and parse the JSONL log file into a list of trade dicts (BACK-01).

        Each line in the JSONL file is parsed independently. Malformed lines are
        skipped with a WARNING log entry rather than raising an exception (T-02-01).

        If the file does not exist, returns an empty list and logs a warning.

        Returns:
            List of trade dicts. If last_n is set, returns only the last N entries.
        """
        import os

        if not os.path.exists(self.log_file):
            logger.warning(f"Log file not found: {self.log_file} — returning empty trade list")
            return []

        trades = []
        with open(self.log_file, "r", encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    trade = json.loads(raw_line)
                    trades.append(trade)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        f"Skipping malformed JSONL line {line_num} in {self.log_file}: {exc}"
                    )

        if self.last_n is not None and self.last_n > 0:
            trades = trades[-self.last_n:]

        logger.debug(f"Loaded {len(trades)} trades from {self.log_file}")
        return trades


@dataclass
class BacktestReport:
    """Aggregated metrics from a backtest run (BACK-02).

    All numeric fields use Decimal for precision. profit_buckets holds
    distribution counts keyed by range label.
    """

    win_rate: Decimal = Decimal("0")
    total_opportunities: Decimal = Decimal("0")
    avg_profit: Decimal = Decimal("0")
    max_profit: Decimal = Decimal("0")
    max_loss: Decimal = Decimal("0")
    profitable_count: Decimal = Decimal("0")
    losing_count: Decimal = Decimal("0")
    profit_buckets: dict = field(default_factory=dict)


def _parse_decimal(value) -> Decimal:
    """Safely parse a value to Decimal, accepting strings and numbers.

    Uses Decimal(str(value)) to avoid float contamination at the log boundary.
    Returns Decimal('0') on failure.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _bucket_label(profit_pct: Decimal) -> str:
    """Return the profit distribution bucket label for a given profit_pct value."""
    if profit_pct < Decimal("0"):
        return "<0"
    elif profit_pct < Decimal("0.5"):
        return "0.0-0.5"
    elif profit_pct < Decimal("1.0"):
        return "0.5-1.0"
    elif profit_pct < Decimal("2.0"):
        return "1.0-2.0"
    else:
        return "2.0+"


def compute_report(trades: list[dict]) -> BacktestReport:
    """Compute aggregated backtest metrics from a list of trade dicts (BACK-02).

    Win/loss determination:
    - Win: profit_ratio > 0 (positive profit after fees)
    - Loss: profit_ratio <= 0

    Metrics computed (all Decimal):
    - win_rate = (profitable_count / total_opportunities) * 100
    - avg_profit = mean of all profit_pct values
    - max_profit = maximum profit_pct
    - max_loss = minimum profit_pct (most negative)
    - profit_buckets: distribution of profit_pct values across labeled ranges

    Args:
        trades: List of trade dicts parsed from JSONL logs.

    Returns:
        BacktestReport with all metrics populated. Returns all-zero report for
        empty input without raising an error.
    """
    if not trades:
        return BacktestReport()

    total = Decimal(str(len(trades)))
    profitable_count = Decimal("0")
    losing_count = Decimal("0")
    profit_pcts: list[Decimal] = []
    buckets: dict[str, int] = {}

    for trade in trades:
        profit_ratio = _parse_decimal(trade.get("profit_ratio", "0"))
        profit_pct = _parse_decimal(trade.get("profit_pct", "0"))

        if profit_ratio > Decimal("0"):
            profitable_count += Decimal("1")
        else:
            losing_count += Decimal("1")

        profit_pcts.append(profit_pct)

        label = _bucket_label(profit_pct)
        buckets[label] = buckets.get(label, 0) + 1

    win_rate = (profitable_count / total) * Decimal("100")
    avg_profit = sum(profit_pcts, Decimal("0")) / total
    max_profit = max(profit_pcts)
    max_loss = min(profit_pcts)

    return BacktestReport(
        win_rate=win_rate,
        total_opportunities=total,
        avg_profit=avg_profit,
        max_profit=max_profit,
        max_loss=max_loss,
        profitable_count=profitable_count,
        losing_count=losing_count,
        profit_buckets=buckets,
    )


def format_report(report: BacktestReport) -> str:
    """Format a BacktestReport as a human-readable multiline string for stdout.

    Includes all metrics and profit distribution table.

    Args:
        report: BacktestReport instance from compute_report().

    Returns:
        Multi-line string suitable for printing to stdout.
    """
    lines = [
        "=" * 52,
        "  XRPL Arbitrage Bot — Backtest Report",
        "=" * 52,
        f"  Total Opportunities : {report.total_opportunities}",
        f"  Win Rate            : {report.win_rate:.2f}%",
        f"  Profitable Trades   : {report.profitable_count}",
        f"  Losing Trades       : {report.losing_count}",
        f"  Avg Profit          : {report.avg_profit:.4f}%",
        f"  Max Profit          : {report.max_profit:.4f}%",
        f"  Max Loss            : {report.max_loss:.4f}%",
        "",
        "  Profit Distribution:",
    ]

    bucket_order = ["<0", "0.0-0.5", "0.5-1.0", "1.0-2.0", "2.0+"]
    for label in bucket_order:
        count = report.profit_buckets.get(label, 0)
        if count > 0 or report.total_opportunities > Decimal("0"):
            bar = "#" * count
            lines.append(f"    {label:>10} : {bar} ({count})")

    lines.append("=" * 52)
    return "\n".join(lines)


def save_report_json(report: BacktestReport, path: str) -> None:
    """Write a BacktestReport as JSON to the given file path.

    Uses json.dumps with default=str to safely serialize Decimal values
    without TypeError (BACK-02).

    Args:
        report: BacktestReport instance to serialize.
        path: Destination file path for the JSON report.
    """
    data = {
        "win_rate": report.win_rate,
        "total_opportunities": report.total_opportunities,
        "avg_profit": report.avg_profit,
        "max_profit": report.max_profit,
        "max_loss": report.max_loss,
        "profitable_count": report.profitable_count,
        "losing_count": report.losing_count,
        "profit_buckets": report.profit_buckets,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str, indent=2))
        logger.info(f"Backtest report saved to {path}")
    except OSError as exc:
        logger.error(f"Failed to save backtest report to {path}: {exc}")
