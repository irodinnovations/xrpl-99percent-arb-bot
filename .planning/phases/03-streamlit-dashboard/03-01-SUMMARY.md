---
phase: 03-streamlit-dashboard
plan: "01"
subsystem: dashboard
tags: [streamlit, dashboard, metrics, auto-refresh]
dependency_graph:
  requires:
    - src/backtester.py (BacktestEngine, compute_report)
    - src/config.py (LOG_FILE)
  provides:
    - src/dashboard.py (runnable Streamlit app)
  affects:
    - requirements.txt (added streamlit, pandas, plotly)
tech_stack:
  added:
    - streamlit>=1.35.0
    - pandas>=2.0.0
    - plotly>=5.18.0
  patterns:
    - Streamlit single-page app with st.set_page_config at top level
    - Auto-refresh via time.sleep(5) + st.rerun() at module level
    - Data loading via BacktestEngine(LOG_FILE).load_trades() + compute_report()
key_files:
  created:
    - src/dashboard.py
  modified:
    - requirements.txt
decisions:
  - "st.rerun() native auto-refresh used — no external autorefresh library needed"
  - "Dark theme deferred to .streamlit/config.toml in Plan 02"
  - "Empty state via st.info() when trades list is empty — no st.metric() calls rendered"
  - "st.set_page_config called once at top level, never inside loop or function"
metrics:
  duration: "1m"
  completed: "2026-04-10"
  tasks_completed: 2
  files_changed: 2
---

# Phase 03 Plan 01: Core Dashboard Shell Summary

Streamlit dashboard app with KPI metrics, empty state handling, and 5-second auto-refresh loop reading live data from xrpl_arb_log.jsonl via BacktestEngine.

## What Was Built

- `src/dashboard.py` — Streamlit single-page app satisfying UI-01, UI-04, UI-05
- `requirements.txt` — streamlit, pandas, plotly added as explicit dependencies

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add streamlit, pandas, plotly to requirements.txt | d7d6e01 | requirements.txt |
| 2 | Create src/dashboard.py — core app with metrics, empty state, auto-refresh | 5b93496 | src/dashboard.py |

## Key Implementation Details

**Auto-refresh pattern** (D-3): The three module-level lines `render_dashboard()` / `time.sleep(5)` / `st.rerun()` form the loop. Streamlit re-executes the entire module on each `st.rerun()` call — no explicit `while True` loop needed.

**Empty state** (D-6): `render_dashboard()` returns early with `st.info()` when `trades` is empty. This means `st.metric()` is never reached for empty data, satisfying the UI-05 requirement exactly.

**KPI cards** (UI-01): Three columns render Win Rate (`{win_rate:.2f}%`), Total Opportunities (`str(int(...))`), and Average Profit (`{avg_profit:.4f}%`) from a `BacktestReport`.

**Placeholder sections**: `st.subheader` + `st.info()` stubs for the table and chart that Plan 02 will replace.

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

| File | Location | Stub | Resolved By |
|------|----------|------|-------------|
| src/dashboard.py | render_dashboard(), after st.divider() | "Table coming in plan 02" | Plan 03-02 |
| src/dashboard.py | render_dashboard(), after second st.divider() | "Chart coming in plan 02" | Plan 03-02 |

These stubs are intentional placeholders — they do not prevent the plan's goal (KPI cards + auto-refresh shell) from being achieved.

## Threat Surface Scan

No new threat surface introduced beyond what is documented in the plan's threat model (T-03-01 through T-03-04). Dashboard is read-only, no wallet actions, no secrets displayed.

## Self-Check: PASSED

- [x] `src/dashboard.py` exists
- [x] `requirements.txt` contains streamlit, pandas, plotly
- [x] Commit d7d6e01 exists
- [x] Commit 5b93496 exists
