# XRPL Arbitrage Bot — Handoff Document
**Date:** 2026-04-13
**Branch:** `claude/recursing-chebyshev`
**Repo:** https://github.com/irodinnovations/xrpl-99percent-arb-bot

---

## The Problem We Found

The bot has been running for 3+ days and found **zero arbitrage opportunities**. After extensive debugging, the root cause is:

**`ripple_path_find` does NOT find circular XRP→IOU→XRP arbitrage loops.**

When `source_account = destination_account` and both amounts are XRP, the XRPL node returns an empty `alternatives` array — every time, at every amount. This is because `ripple_path_find` is designed to find cheapest payment paths from A to B, not circular routes back to the same currency.

### Diagnostic Proof (April 13, 2026)

| Test | Result |
|------|--------|
| XRP→XRP self-payment (1, 5, 10, 50 XRP) | **0 alternatives** always |
| XRP→USD (Bitstamp) | 1 alternative, costs 0.748 XRP |
| XRP→USD (GateHub) | 1 alternative, costs 0.744 XRP |
| XRP→RLUSD (Ripple) | 1 alternative, costs 0.741 XRP |
| HTTP RPC endpoint | Clio server (still s1.ripple.com in .env) |

The XRP→IOU direction works. The problem is the bot assumes a single `ripple_path_find` call can discover a full circular route, but it can't.

---

## What Needs to Change: Two-Leg Arbitrage

Instead of one `ripple_path_find` call looking for XRP→XRP loops, the bot needs **two-leg scanning**:

### Approach 1: Two-Leg Path Finding
1. **Leg 1 (buy IOU):** `ripple_path_find` — How much XRP to get 1 USD? (e.g., 0.748 XRP via Bitstamp)
2. **Leg 2 (sell IOU):** `ripple_path_find` — How much XRP do I get for 1 USD? (e.g., via GateHub)
3. **Arbitrage check:** If sell_xrp > buy_xrp + fees, there's profit

This exploits price differences between **different issuers of the same token** (e.g., Bitstamp USD vs GateHub USD) or between **AMM pools and order books**.

### Approach 2: Order Book Scanning (`book_offers`)
- Use `book_offers` API to get bid/ask prices for each IOU/XRP pair
- Compare effective prices across issuers
- Faster than path_find, can check many pairs quickly

### Approach 3: Triangular Arbitrage
- XRP → Token_A → Token_B → XRP
- Three legs, each using `ripple_path_find` or `book_offers`
- More complex but can find opportunities the two-leg approach misses

**Recommended:** Start with Approach 1 (two-leg), it's the simplest refactor.

---

## Current State of the Codebase

### Files and What They Do

| File | Purpose | Status |
|------|---------|--------|
| `main.py` | Bot entry point, ledger-close loop | Working, needs pathfinder strategy change |
| `src/pathfinder.py` | Builds and parses ripple_path_find | **Needs rewrite** — current XRP→XRP approach doesn't work |
| `src/connection.py` | WebSocket connection + auto-reconnect | Working fine |
| `src/executor.py` | Client-side sign + submit transactions | Working, uses client-side signing |
| `src/profit_math.py` | Decimal profit/slippage/fee calculations | Working correctly |
| `src/config.py` | .env loading, Decimal constants | Working, has POSITION_TIERS |
| `src/safety.py` | CircuitBreaker + Blacklist | Working |
| `src/simulator.py` | `simulate` RPC pre-flight check | Working (untested with real txs) |
| `src/trade_logger.py` | JSONL trade logging | Working |
| `src/telegram_alerts.py` | Optional Telegram notifications | Working |
| `src/ai_brain.py` | Optional Claude post-trade analysis | Working |
| `src/dashboard.py` | Streamlit dashboard | Working |
| `src/backtester.py` | Backtesting from JSONL logs | Working |
| `scripts/setup_trust_lines.py` | One-time trust line setup | Done, 27 lines set |
| `scripts/diagnose_pathfind.py` | Diagnostic tool | Confirmed the problem |

### Bug Fixes Already Applied (PRs #1-4)

1. **Circuit breaker fix** — `reference_balance` now set on first balance fetch
2. **Client-side signing** — wallet seed never sent to RPC node
3. **Fee math fix** — network fee is ratio relative to trade size (`NETWORK_FEE / input_xrp`)
4. **Multi-tier scanning** — probes at 1%, 5%, 10% of balance
5. **Trust lines** — 27 Tier 1-3 tokens with tfSetNoRipple
6. **RPC endpoint** — WebSocket switched to `wss://s2.ripple.com`
7. **Debug logging** — pathfinder now logs raw alternative counts

### Known Issues Still Open

1. **RPC URL on VPS is still s1.ripple.com (Clio)** — .env needs `XRPL_RPC_URL=https://s2.ripple.com:51234`
   - Only affects `executor.py` simulate calls and `setup_trust_lines.py`, NOT the WebSocket path_find
2. **LOG_LEVEL on VPS is currently DEBUG** — set back to INFO after debugging
3. **No PR created yet for latest diagnostic commit** — on `claude/recursing-chebyshev` branch

---

## VPS Details

- **Host:** Hostinger KVM 1, srv1309513
- **Path:** `/opt/xrplbot/`
- **User:** `xrplbot` (service account)
- **Service:** `systemd` unit `xrplbot`
- **Python:** venv at `/opt/xrplbot/venv/`
- **Wallet:** `r3yPcfPJuPkG1AJxNxbUpQHZVfEaa8VPKq`
- **Balance:** ~100 XRP (73 liquid after 27 XRP trust line reserves)
- **Trust lines:** 27 (Tiers 1-3, all with NoRipple)

### Useful VPS Commands
```bash
# Check bot status
sudo systemctl status xrplbot

# View logs
sudo journalctl -u xrplbot --since "5 min ago" --no-pager

# Restart
sudo systemctl restart xrplbot

# Run scripts
sudo -u xrplbot /opt/xrplbot/venv/bin/python scripts/diagnose_pathfind.py

# Deploy latest code
sudo -u xrplbot bash -c 'cd /opt/xrplbot && git pull origin claude/recursing-chebyshev'
sudo systemctl restart xrplbot
```

---

## Config Values (from .env.example)

| Variable | Current Value | Notes |
|----------|--------------|-------|
| XRPL_WS_URL | `wss://s2.ripple.com` | Full rippled node |
| XRPL_RPC_URL | `https://s1.ripple.com` (VPS) | **Should be s2.ripple.com:51234** |
| DRY_RUN | `True` | Paper trading mode |
| PROFIT_THRESHOLD | `0.006` (0.6%) | Min profit after fees |
| MAX_POSITION_PCT | `0.05` (5%) | Max trade size |
| SLIPPAGE_BASE | `0.003` (0.3%) | Base slippage buffer |
| POSITION_TIERS | `[0.01, 0.05, 0.10]` | Multi-tier scan sizes |
| DAILY_LOSS_LIMIT_PCT | `0.02` (2%) | Circuit breaker threshold |

---

## What to Do Next (Priority Order)

### 1. Rewrite PathFinder for Two-Leg Scanning (Critical)
The `scan()` method in `src/pathfinder.py` needs to:
- Loop through pairs of IOU tokens from trust lines
- For each pair: call `ripple_path_find` for XRP→IOU and IOU→XRP
- Compare effective rates to find arbitrage
- This is the only way to find opportunities — the current approach is fundamentally broken

### 2. Consider `book_offers` API (Enhancement)
- Faster than two path_find calls per pair
- Can scan order book depth directly
- Good complement to path_find approach

### 3. Fix VPS .env RPC URL (Quick Fix)
```bash
# On VPS, edit .env:
sudo -u xrplbot nano /opt/xrplbot/.env
# Change: XRPL_RPC_URL=https://s2.ripple.com:51234
# Change: LOG_LEVEL=INFO
```

### 4. Merge Branch to Main
The `claude/recursing-chebyshev` branch has all fixes. Create PR and merge.

---

## Architecture Notes for the Rewrite

The `Opportunity` dataclass and everything downstream (executor, simulator, safety, logging) should stay the same. Only the **scanning strategy** in `pathfinder.py` needs to change.

The new flow:
```
For each IOU token (27 trust lines):
    1. buy_cost  = ripple_path_find(XRP → IOU, amount=X)
    2. sell_yield = ripple_path_find(IOU → XRP, amount=X)
    3. if sell_yield > buy_cost * (1 + threshold):
         → Create Opportunity(input_xrp=buy_cost, output_xrp=sell_yield, ...)
```

Cross-issuer variant (more opportunities):
```
For each pair of issuers of the same currency (e.g., USD Bitstamp vs USD GateHub):
    1. buy from cheaper issuer
    2. sell to more expensive issuer
    3. profit = price difference - fees - slippage
```

The XRPL handles the atomic swap — a single Payment transaction with paths can execute the full round-trip.
