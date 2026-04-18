# Phase D Runbook — Empirical Validation

Phase D is the last gate before live trading. There is no new code to write;
this document walks through verifying the two-leg rewrite against real XRPL
infrastructure.

## Sequence

1. **VPS preflight** (~5 min): confirm the new code runs in DRY_RUN.
2. **48h paper trading** (2 days): let the bot run against live mainnet in
   DRY_RUN mode and watch the logs.
3. **User review checkpoint**: review paper-trade results with Claude.
4. **(Only after checkpoint) Phase E**: flip `DRY_RUN=False` on a small
   wallet balance.

Do **not** skip to live trading. The 48h paper window is the only way to
catch issues the unit tests can't model (rate timing, actual market depth,
race conditions between legs, rippled response edge cases).

---

## Step 1: VPS preflight

Deploy the `claude/two-leg-rewrite` branch to the VPS and run the extended
preflight.

```bash
# On VPS
sudo -u xrplbot bash -c "
  cd /opt/xrplbot &&
  git fetch origin claude/two-leg-rewrite &&
  git checkout claude/two-leg-rewrite &&
  git pull origin claude/two-leg-rewrite &&
  venv/bin/pip install -r requirements.txt
"

# Run preflight (all 8 checks)
sudo -u xrplbot bash -c "
  cd /opt/xrplbot &&
  venv/bin/python -m scripts.preflight_check
"
```

**Expected output:** all 8 checks `[PASS]`, including:

- `two_leg_pipeline` — `[PASS] executor.execute() returned ... without raising`
  (result can be True or False; what matters is no exception)
- `startup_drain` — `[PASS] Startup drain guard ran without raising`

If any check fails, **stop**. Diagnose before proceeding.

---

## Step 2: 48h paper trading

Ensure `DRY_RUN=True` in `/opt/xrplbot/.env`, then start the service:

```bash
sudo systemctl restart xrplbot
sudo systemctl status xrplbot
sudo journalctl -u xrplbot -f --since "5 minutes ago"
```

Leave it running for at least 24 hours, ideally 48. The bot will:

- Scan every ledger close via `book_changes` stream
- Pre-simulate every opportunity that crosses the 1% threshold
- Log paper trades on sim success
- **Never submit a real transaction** (DRY_RUN gate)

### What to watch for (green flags)

- **Opportunities surfaced**: even one or two per day is fine for a quiet
  market. Zero over 48h suggests the threshold is too strict or rates are
  flat.
- **Most sims pass**: `Simulation passed: tesSUCCESS` dominates the log
  over `Simulation failed: ...`. A few `tecPATH_DRY` is normal (book moved
  after we computed the opp).
- **No `temBAD_SEND_XRP_MAX`**: this was the old bug. Should be zero after
  the two-leg rewrite. If you see even one, the executor is still building
  the old shape somewhere.
- **No uncaught exceptions**: `grep -i traceback journal.log` should
  return nothing.

### What to watch for (red flags)

- **Repeated `Simulation failed: tecPATH_DRY` on the same route**: legitimate
  transient issue, but after 3 within 1 hour the route auto-blacklists for
  24h (by design). If you see blacklists piling up, the thresholds may need
  to drop to match current market spreads.
- **Exceptions from `_submit_and_wait` or `_wait_for_validation`**: these
  paths only fire in LIVE mode — seeing errors in DRY_RUN means a bug.
- **Circuit breaker halts**: should never happen in DRY_RUN since no real
  P&L is booked. If it halts, investigate.

### Log hygiene

The bot writes structured JSON to `xrpl_arb_log.jsonl`. To count paper
trades by outcome:

```bash
cat /opt/xrplbot/xrpl_arb_log.jsonl | \
  jq -r 'select(.dry_run == true) | [.leg1_sim_result, .leg2_sim_result] | @csv' | \
  sort | uniq -c | sort -rn
```

Expected: `tesSUCCESS,tesSUCCESS` should be most common. A sprinkle of
`tesSUCCESS,tecPATH_DRY` is normal (leg 1 sims clean but leg 2 sim trips
because the account doesn't yet hold the IOU — this is the known mainnet
limitation of simulating a chained trade).

---

## Step 3: Checkpoint with Claude

After 48h of paper trading, collect:

1. Total `tesSUCCESS,tesSUCCESS` paper trades
2. Total failed sims and their error codes
3. Any unusual log lines, tracebacks, or halts
4. Wallet balance drift (should be exactly zero — no real trades)

Hand these numbers to Claude in a new session. It will:

- Compare simulate success rate against the design-doc expectations
- Identify any config tuning needed (e.g., threshold too high / too low)
- Decide whether you're ready for Phase E

---

## Step 4: Phase E (after approval only)

Once Claude gives the go-ahead:

1. Move a **small amount** of XRP to the bot wallet (start with 50–100 XRP).
2. Set `MAX_POSITION_PCT=0.02` (it already defaults to 2% for probation).
3. Set `DRY_RUN=False` in `/opt/xrplbot/.env`.
4. Restart the service. Watch logs closely for the first live trade.
5. The bot will self-regulate from here — circuit breaker, blacklist,
   and recovery flow handle all failure modes without intervention.

---

## Recovery during Phase D

If the bot crashes for any reason during the 48h window, the startup
recovery guard runs automatically on next boot. It fetches trust lines,
drains any non-zero IOU balances via tfPartialPayment, and only starts
scanning after the wallet is clean.

In DRY_RUN this is effectively a no-op (no real IOU is acquired). In
Phase E it's the primary safety net for unattended operation.
