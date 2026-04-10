# Roadmap: XRPL 99%+ Arbitrage Bot

## Overview

Four phases take this project from zero to a live VPS-deployed arbitrage bot. Phase 1 builds the complete core engine — connection, pathfinding, profit math, simulation gating, DRY_RUN mode, circuit breakers, Telegram, and logging. Phase 2 adds intelligence — backtesting against historical data and an async Claude AI brain for post-trade pattern review. Phase 3 wraps it in a Streamlit read-only dashboard so you can watch the bot work in real time. Phase 4 ships it to Hostinger — systemd service, non-root user, coexistence with OpenClaw, and a complete deployment guide.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Core Bot Engine** - Working bot that scans, simulates, and executes (or paper-trades) with full safety controls
- [ ] **Phase 2: Backtester + AI Brain** - Historical replay backtesting and async Claude post-trade pattern analysis
- [ ] **Phase 3: Streamlit Dashboard** - Real-time read-only web dashboard fed from the shared JSONL log
- [ ] **Phase 4: Deployment** - systemd service, non-root user, .env.example, README, and Hostinger coexistence guide

## Phase Details

### Phase 1: Core Bot Engine
**Goal**: The bot runs on mainnet, scans for arbitrage opportunities, validates every candidate through live ledger simulation, and either logs a paper trade or executes a live one — with circuit breakers and Telegram alerts throughout
**Depends on**: Nothing (first phase)
**Requirements**: BOT-01, BOT-02, BOT-03, BOT-04, BOT-05, BOT-06, BOT-07, DRY-01, DRY-02, DRY-03, DRY-04, SAFE-01, SAFE-02, SAFE-03, SAFE-04, SAFE-05, LIVE-01, LIVE-02, LIVE-03, TELE-01, TELE-02, TELE-03, LOG-01, LOG-02, LOG-03, LOG-04
**Success Criteria** (what must be TRUE):
  1. Bot connects to XRPL mainnet and logs a heartbeat every ledger close (~3-5 seconds) without dropping
  2. Running with DRY_RUN=True, the bot finds an opportunity and logs a "would execute" entry to xrpl_arb_log.jsonl with correct profit math
  3. No trade proceeds to execution (paper or live) unless simulate RPC returns tesSUCCESS
  4. Bot sends a Telegram alert when an opportunity is detected, including profit percentage and amounts
  5. Bot halts scanning for 24 hours if cumulative daily loss reaches 2% of account balance
**Plans**: 5 plans

Plans:
- [x] 01-01: XRPL connection layer — WebSocket client with auto-reconnect and ledger-close stream
- [x] 01-02: Pathfinder + profit math — ripple_path_find integration, Decimal profit formula, slippage buffer, position sizing
- [ ] 01-03: Simulation gate + execution — simulate RPC validation, DRY_RUN mode, autofill_and_sign live path, post-trade validation
- [x] 01-04: Safety systems — circuit breakers, blacklist, SAFE-04 Decimal enforcement, SAFE-05 .env seed loading
- [x] 01-05: Telegram + logging — alert formatting, JSONL log writer, console logging

### Phase 2: Backtester + AI Brain
**Goal**: Historical data can be replayed to measure strategy win rate, and every executed trade (paper or live) gets an async Claude review that suggests threshold adjustments without blocking the main loop
**Depends on**: Phase 1
**Requirements**: BACK-01, BACK-02, BACK-03, AI-01, AI-02, AI-03, AI-04
**Success Criteria** (what must be TRUE):
  1. Running `python backtest.py` produces a report showing win rate, total opportunities found, and average profit per opportunity
  2. After every paper trade, the bot fires an async Claude call and logs the AI response (suggestion, new_threshold recommendation, reasoning) without slowing ledger scanning
  3. Bot continues operating normally when ANTHROPIC_KEY is absent from .env
**Plans**: TBD

Plans:
- [ ] 02-01: Backtesting module — historical ledger data replay, pathfinding replay, win rate and profit report
- [ ] 02-02: AI brain integration — async Claude API call after each trade, structured JSON response parsing, optional/graceful-skip behavior

### Phase 3: Streamlit Dashboard
**Goal**: A browser-based read-only dashboard auto-refreshes from the shared JSONL log and shows the bot's live win rate, recent opportunities, and profit distribution
**Depends on**: Phase 1
**Requirements**: UI-01, UI-02, UI-03, UI-04, UI-05
**Success Criteria** (what must be TRUE):
  1. Opening the dashboard in a browser shows current win rate, total opportunities, and average profit — updated automatically every 5 seconds
  2. A table of the 20 most recent opportunities is visible with all relevant fields
  3. A Plotly profit distribution histogram renders correctly from real log data
  4. Dashboard shows a clean empty state message when xrpl_arb_log.jsonl does not exist yet
**Plans**: TBD

Plans:
- [ ] 03-01: Streamlit app — log reader, metrics calculations, auto-refresh, empty state handling
- [ ] 03-02: Charts and table — Plotly histogram, recent opportunities data table, layout polish
**UI hint**: yes

### Phase 4: Deployment
**Goal**: The bot runs as a hardened systemd service under a non-root user on the Hostinger VPS, coexists with OpenClaw Docker, and a complete README guides anyone through the full setup from SSH to live trading
**Depends on**: Phase 3
**Requirements**: DEP-01, DEP-02, DEP-03, DEP-04, DEP-05, DEP-06, DEP-07
**Success Criteria** (what must be TRUE):
  1. `sudo systemctl start xrplbot` starts the bot as the xrplbot non-root user with Restart=always and appropriate VPS resource limits
  2. The bot and OpenClaw Docker project run simultaneously on Hostinger KVM 1 without port conflicts or resource contention
  3. A new user can follow README alone — from SSH login through 7-day paper trading review to switching to live with 10-20 XRP
  4. .env.example documents every environment variable the bot reads, with inline explanations
**Plans**: TBD

Plans:
- [ ] 04-01: systemd service + user setup — xrplbot user creation, service file with resource limits, enable/start workflow
- [ ] 04-02: .env.example + README — all env vars documented, Hostinger deployment guide, OpenClaw coexistence notes, 7-day paper review checklist, live-trading switchover instructions

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Core Bot Engine | 4/5 | In Progress|  |
| 2. Backtester + AI Brain | 0/2 | Not started | - |
| 3. Streamlit Dashboard | 0/2 | Not started | - |
| 4. Deployment | 0/2 | Not started | - |
