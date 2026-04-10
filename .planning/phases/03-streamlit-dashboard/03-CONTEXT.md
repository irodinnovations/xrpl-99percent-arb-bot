# Phase 3: Streamlit Dashboard - Context

**Gathered:** 2026-04-10
**Status:** Ready for planning
**Mode:** Smart discuss (autonomous)

<domain>
## Phase Boundary

A browser-based read-only dashboard auto-refreshes from the shared JSONL log and shows the bot's live win rate, recent opportunities, and profit distribution.

Requirements: UI-01, UI-02, UI-03, UI-04, UI-05

Success Criteria:
1. Opening the dashboard in a browser shows current win rate, total opportunities, and average profit — updated automatically every 5 seconds
2. A table of the 20 most recent opportunities is visible with all relevant fields
3. A Plotly profit distribution histogram renders correctly from real log data
4. Dashboard shows a clean empty state message when xrpl_arb_log.jsonl does not exist yet

</domain>

<decisions>
## Implementation Decisions

### 1. Dashboard Layout
**Decision:** Single-page Streamlit app with three sections: metrics row at top (3 columns), recent opportunities table in the middle, profit histogram at bottom.
**Rationale:** Standard dashboard layout, matches the 3 success criteria naturally. Streamlit's column layout makes this trivial.

### 2. Theme & Styling
**Decision:** Use Streamlit's default dark theme. No custom CSS beyond st.set_page_config for title/icon.
**Rationale:** The project constraints say keep it simple. Streamlit's built-in dark theme is clean and readable. Dashboard is read-only for VPS access — no need for custom branding.

### 3. Auto-Refresh Mechanism
**Decision:** Use st.rerun() with time.sleep(5) inside a while loop, controlled by st.empty() placeholders. Show "Last updated: HH:MM:SS" timestamp.
**Rationale:** Streamlit's st.autorefresh (from streamlit-autorefresh) would add a dependency. Native approach with st.rerun() keeps dependencies minimal per project constraints.

### 4. Data Reading
**Decision:** Reuse BacktestEngine from src/backtester.py to parse the JSONL log, since it already handles malformed lines and Decimal parsing. Import compute_report for metrics.
**Rationale:** DRY — the backtester already has robust JSONL parsing with error handling (T-02-01 mitigated). Avoids duplicate parsing code.

### 5. Profit Histogram
**Decision:** Plotly Express histogram with the same bucket ranges as BacktestReport.profit_distribution ("<0", "0.0-0.5", "0.5-1.0", "1.0-2.0", "2.0+"). Dark theme compatible.
**Rationale:** UI-03 specifically requires Plotly. Using the same buckets as the backtester keeps reporting consistent.

### 6. Empty State
**Decision:** Show a centered info message "No trading data yet. Start the bot to begin collecting data." with st.info() when log file doesn't exist or has zero entries.
**Rationale:** UI-05 requires graceful empty state. st.info() is the standard Streamlit pattern.

### 7. Streamlit Port
**Decision:** Default to port 8501 (Streamlit default). Configurable via STREAMLIT_PORT env var if needed to avoid conflicts with OpenClaw.
**Rationale:** OpenClaw Docker typically uses ports 80/443/3000. Streamlit's 8501 shouldn't conflict. Env var provides escape hatch.

</decisions>

<code_context>
## Existing Code Insights

- **src/backtester.py** — BacktestEngine.load_trades() parses JSONL, compute_report() calculates metrics. Both handle empty/missing files gracefully.
- **src/config.py** — LOG_FILE constant points to xrpl_arb_log.jsonl. Pattern for env var loading established.
- **src/trade_logger.py** — Defines the JSONL entry format with all fields (timestamp, profit_pct, profit_ratio, input_xrp, output_xrp, simulated_output, dry_run, simulation_result, hash, error).
- **Dependencies** — streamlit, pandas, plotly already in project constraints list.

</code_context>

<specifics>
## Specific Ideas

- Reuse BacktestEngine and compute_report from src/backtester.py for data loading and metrics
- Recent opportunities table should show: timestamp, profit_pct, input_xrp, output_xrp, dry_run, simulation_result
- Metrics row: Win Rate (%), Total Opportunities (#), Average Profit (%)
- Use st.metric() for the three KPI cards — provides built-in delta indicators if needed later

</specifics>

<deferred>
## Deferred Ideas

- Authentication/auth on dashboard (out of scope per PROJECT.md)
- AI review insights panel (could be v2 feature)
- Trade execution controls (dashboard is read-only per PROJECT.md)
- WebSocket live updates (overkill for 5s refresh interval)

</deferred>
