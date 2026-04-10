"""XRPL Arbitrage Bot — Streamlit live dashboard.

UI-01: Three KPI metric cards (Win Rate, Total Opportunities, Average Profit).
UI-04: Auto-refresh every 5 seconds via st.rerun().
UI-05: Empty state message when no log data exists.

Run with:
    streamlit run src/dashboard.py
"""

import time
from datetime import datetime

import streamlit as st

from src.backtester import BacktestEngine, compute_report
from src.config import LOG_FILE

# Page config must be the first Streamlit call — never inside a loop or function.
st.set_page_config(
    page_title="XRPL Arb Bot",
    page_icon=":chart_with_upward_trend:",
    layout="wide",
)


def load_dashboard_data() -> tuple:
    """Load trades from log file and compute metrics report.

    Returns:
        Tuple of (trades list, BacktestReport). Both are safe for empty/missing file.
    """
    engine = BacktestEngine(LOG_FILE)
    trades = engine.load_trades()
    report = compute_report(trades)
    return trades, report


def render_dashboard() -> None:
    """Render one frame of the dashboard: title, timestamp, metrics or empty state."""
    st.title("XRPL Arbitrage Bot — Live Dashboard")

    trades, report = load_dashboard_data()

    # Timestamp shown every cycle so the user can confirm refresh is working (UI-04).
    st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

    if not trades:
        # Empty state: shown when log file is missing or empty (UI-05, D-6).
        st.info("No trading data yet. Start the bot to begin collecting data.")
        return

    # Three KPI metric cards (UI-01, design spec D-1).
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Win Rate", f"{report.win_rate:.2f}%")
    with col2:
        st.metric("Total Opportunities", str(int(report.total_opportunities)))
    with col3:
        st.metric("Average Profit", f"{report.avg_profit:.4f}%")

    # Placeholder sections — Plan 02 will fill these in.
    st.divider()
    st.subheader("Recent Opportunities")
    st.info("Table coming in plan 02")

    st.divider()
    st.subheader("Profit Distribution")
    st.info("Chart coming in plan 02")


# Auto-refresh loop (D-3 decision: native st.rerun(), no external library).
# Streamlit re-executes this module on every rerun, so these three lines form the loop.
render_dashboard()
time.sleep(5)
st.rerun()
