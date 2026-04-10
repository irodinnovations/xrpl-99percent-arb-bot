"""Backtest CLI — replay paper trading logs to measure strategy performance (BACK-03).

Usage:
    python backtest.py [--log-file PATH] [--last-n N]

Options:
    --log-file  Path to the JSONL trade log file (default: xrpl_arb_log.jsonl from config)
    --last-n    Analyze only the last N trade entries (default: all entries)
"""

import argparse
import sys

from src.backtester import BacktestEngine, compute_report, format_report, save_report_json
from src.config import LOG_FILE

REPORT_OUTPUT_PATH = "backtest_report.json"


def main() -> None:
    """Parse arguments and run the backtester."""
    parser = argparse.ArgumentParser(
        prog="backtest.py",
        description="Replay paper trading logs to measure XRPL arbitrage strategy performance.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=LOG_FILE,
        help=f"Path to JSONL trade log file (default: {LOG_FILE})",
    )
    parser.add_argument(
        "--last-n",
        type=int,
        default=None,
        metavar="N",
        help="Analyze only the last N trade entries (default: all entries)",
    )

    args = parser.parse_args()

    engine = BacktestEngine(log_file=args.log_file, last_n=args.last_n)
    trades = engine.load_trades()

    if not trades:
        print(f"No trades found in {args.log_file}")
        sys.exit(0)

    report = compute_report(trades)
    print(format_report(report))
    save_report_json(report, REPORT_OUTPUT_PATH)
    print(f"Report saved to {REPORT_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
