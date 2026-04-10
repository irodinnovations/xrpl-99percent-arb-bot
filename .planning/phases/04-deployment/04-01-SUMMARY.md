---
phase: 04-deployment
plan: "01"
subsystem: deployment
tags: [systemd, linux, vps, hardening, xrplbot]
dependency_graph:
  requires: []
  provides: [deploy/xrplbot.service, deploy/setup.sh]
  affects: [VPS deployment workflow, OpenClaw coexistence]
tech_stack:
  added: [systemd service unit, bash provisioning script]
  patterns: [EnvironmentFile for secrets, CPUQuota/MemoryMax resource caps, ProtectSystem=strict hardening]
key_files:
  created:
    - deploy/xrplbot.service
    - deploy/setup.sh
  modified: []
key_decisions:
  - "CPUQuota=80% chosen to leave ~20% CPU headroom for OpenClaw Docker and OS on single-core VPS"
  - "MemoryMax=512M set as safe ceiling — bot uses ~100-150MB; prevents runaway without threatening OpenClaw"
  - "MemorySwapMax=0 prevents swap usage which adds latency to a trading bot"
  - "nologin shell (/usr/sbin/nologin) on xrplbot user — cannot SSH in even with key"
  - "ProtectSystem=strict + ReadWritePaths=/opt/xrplbot — bot can only write within its own directory"
  - "EnvironmentFile=/opt/xrplbot/.env loads all secrets without exposing them in process list"
  - "After=network-online.target ensures XRPL WebSocket has internet before service start"
  - "RestartSec=10 prevents tight restart loops on repeated crashes"
  - "setup.sh is idempotent — checks user/venv existence before creating, safe to re-run"
metrics:
  duration: "4m"
  completed_date: "2026-04-10"
  tasks_completed: 2
  files_created: 2
  files_modified: 0
requirements_satisfied: [DEP-01, DEP-02, DEP-05]
---

# Phase 04 Plan 01: VPS Deployment — systemd Service and Setup Script Summary

**One-liner:** Systemd service with xrplbot non-root user, nologin shell, CPUQuota/MemoryMax resource caps, and EnvironmentFile secret loading — with a one-shot idempotent setup.sh for Hostinger KVM 1.

## What Was Built

Two deployment artifacts committed to `deploy/`:

1. **`deploy/xrplbot.service`** — systemd unit file that runs the bot as a hardened, auto-restarting service
2. **`deploy/setup.sh`** — one-shot bash provisioning script (idempotent, safe to re-run) that sets up the xrplbot user, venv, and enables the service

## Task Results

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create systemd service unit file | bc777ae | deploy/xrplbot.service |
| 2 | Create setup.sh provisioning script | 1d998ef | deploy/setup.sh |

## Key Decisions Made

### CPUQuota and MemoryMax Values

- **CPUQuota=80%**: On a 1-core VPS, this caps the bot at 0.8 CPU, leaving 20% for OpenClaw Docker and OS overhead. The bot is I/O-bound (WebSocket + HTTP), not CPU-bound, so it rarely hits this limit.
- **MemoryMax=512M**: Bot actual usage is ~100-150MB. 512M is a 3x safety margin without threatening OpenClaw's ~1-2GB allocation.
- **MemorySwapMax=0**: Trading bots should never swap — swap latency could cause missed ledger windows or stale price data.

### Security Hardening Applied

| Directive | Purpose | Threat |
|-----------|---------|--------|
| `User=xrplbot` + `--shell /usr/sbin/nologin` | No interactive login possible | T-04-01 |
| `NoNewPrivileges=true` | Blocks setuid privilege escalation | T-04-01 |
| `ProtectSystem=strict` + `ReadWritePaths=/opt/xrplbot` | Bot can only write to its own directory | T-04-02 |
| `PrivateTmp=true` | Bot gets isolated /tmp — cannot read other processes' temp files | T-04-02 |
| `ProtectHome=true` | Bot cannot read user home directories | T-04-02 |
| `EnvironmentFile=/opt/xrplbot/.env` + `chmod 600` | Secrets loaded without appearing in process list; only xrplbot can read | T-04-02 |
| `MemoryMax=512M` + `CPUQuota=80%` | Hard resource caps prevent DoS to OpenClaw | T-04-03 |
| venv owned by xrplbot | pip install never touches system Python | T-04-05 |

### OpenClaw Coexistence

The bot is a **pure outbound WebSocket client** — it binds to no network ports. OpenClaw Docker uses its own bridge network. There is zero port conflict risk. The only shared resources are CPU and RAM, which are managed by the service file's `CPUQuota` and `MemoryMax` directives. This is documented explicitly in the `setup.sh` header comment.

### EnvironmentFile Pattern

All environment variables from `src/config.py` (XRPL_SECRET, XRPL_WS_URL, XRPL_RPC_URL, DRY_RUN, PROFIT_THRESHOLD, MAX_POSITION_PCT, DAILY_LOSS_LIMIT_PCT, SLIPPAGE_BASE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_KEY, AI_REVIEWS_FILE, LOG_FILE, LOG_LEVEL) are loaded via `EnvironmentFile=/opt/xrplbot/.env`. This means secrets never appear in `ps aux` output or `/proc/{pid}/environ` readable by other users.

## Verification Results

All 5 plan checks passed:

1. `ls -la deploy/` — both files present
2. `bash -n deploy/setup.sh` — exits 0 (syntax OK)
3. `grep -c "xrplbot" deploy/xrplbot.service` — returns 7 (>= 3 required)
4. `grep "CPUQuota=80%" deploy/xrplbot.service` — match found
5. `grep "MemoryMax=512M" deploy/xrplbot.service` — match found

## Deployment Flow (when user runs on Hostinger VPS)

```
sudo bash deploy/setup.sh
    -> creates xrplbot user (nologin)
    -> creates /opt/xrplbot
    -> [PAUSE] user copies files + creates .env
    -> chown -R xrplbot:xrplbot /opt/xrplbot
    -> chmod 600 /opt/xrplbot/.env
    -> creates venv as xrplbot, installs requirements
    -> cp service file to /etc/systemd/system/
    -> systemctl daemon-reload && systemctl enable xrplbot

sudo systemctl start xrplbot
sudo journalctl -u xrplbot -f
```

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — deployment artifacts are complete and self-contained. The ExecStart path (`/opt/xrplbot/venv/bin/python main.py`) correctly targets the bot entry point verified in `main.py`.

## Self-Check: PASSED

- deploy/xrplbot.service: FOUND
- deploy/setup.sh: FOUND
- Commit bc777ae: verified (feat(04-01): add systemd service unit file)
- Commit 1d998ef: verified (feat(04-01): add one-shot VPS provisioning script)
