"""XRPL Arbitrage Bot — Streamlit live dashboard.

UI-01: Three KPI metric cards (Win Rate, Total Opportunities, Average Profit).
UI-02: Recent opportunities table — last 20 trades, newest first.
UI-03: Profit distribution bar chart using BacktestReport.profit_buckets.
UI-04: Auto-refresh every 5 seconds via st.rerun().
UI-05: Empty state message when no log data exists.

Run with:
    streamlit run src/dashboard.py
"""

import time
from datetime import datetime

import pandas as pd
import plotly.express as px
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

    # Recent trades table (UI-02) — last 20, newest first (per D-1 layout).
    st.divider()
    st.subheader("Recent Opportunities")

    # Build recent trades DataFrame — slice last 20, reverse to newest-first.
    recent = trades[-20:][::-1]

    TABLE_COLUMNS = ["timestamp", "profit_pct", "input_xrp", "output_xrp", "dry_run", "simulation_result"]

    rows = []
    for t in recent:
        rows.append({col: t.get(col, "") for col in TABLE_COLUMNS})

    df = pd.DataFrame(rows, columns=TABLE_COLUMNS)

    # Format profit_pct to 4 decimal places for readability (T-03-05 mitigated via float()).
    if "profit_pct" in df.columns and not df.empty:
        df["profit_pct"] = df["profit_pct"].apply(
            lambda v: f"{float(v):.4f}" if v != "" else ""
        )

    st.dataframe(df, use_container_width=True, hide_index=True)

    # Profit distribution bar chart (UI-03) — same bucket labels as BacktestReport.
    st.divider()
    st.subheader("Profit Distribution")

    # Fixed bucket order (D-5 decision) — never sort alphabetically.
    BUCKET_ORDER = ["<0", "0.0-0.5", "0.5-1.0", "1.0-2.0", "2.0+"]
    counts = [report.profit_buckets.get(label, 0) for label in BUCKET_ORDER]

    fig = px.bar(
        x=BUCKET_ORDER,
        y=counts,
        labels={"x": "Profit Range (%)", "y": "Trade Count"},
        title="Profit Distribution",
        color_discrete_sequence=["#00CC96"],  # green bars, readable on dark theme
    )
    fig.update_layout(
        xaxis={"categoryorder": "array", "categoryarray": BUCKET_ORDER},
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)


# Auto-refresh loop (D-3 decision: native st.rerun(), no external library).
# Streamlit re-executes this module on every rerun, so these three lines form the loop.
render_dashboard()
time.sleep(5)
st.rerun()
