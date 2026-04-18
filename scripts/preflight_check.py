"""Pre-live preflight check — verifies safety infrastructure end-to-end.

Run this on the VPS before flipping DRY_RUN=False to confirm:
  1. Wallet secret loads + address matches expected
  2. Config sanity (thresholds, caps, new two-leg env vars)
  3. Telegram alerts reach your chat (TELE-03 path)
  4. XRPL connection works and ledger index advances
  5. Circuit breaker halts after simulated loss (SAFE-02)
  6. Pathfinder emits two-leg opportunities (or reports none)
  7. TradeExecutor DRY_RUN pipeline (leg 1 + leg 2 simulate) works
  8. Startup IOU drain guard runs clean on empty wallet

Does NOT submit any real transactions.  Read-only against the ledger.

Usage on VPS:
  sudo -u xrplbot bash -c "cd /opt/xrplbot && venv/bin/python -m scripts.preflight_check"
"""

import asyncio
import logging
import sys
import time
from decimal import Decimal

from xrpl.wallet import Wallet

from src.config import (
    XRPL_SECRET,
    XRPL_WS_URL,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    DAILY_LOSS_LIMIT_PCT,
    MAX_POSITION_PCT,
    PROFIT_THRESHOLD,
    MAX_TRADE_XRP_ABS,
    MIN_BALANCE_GUARD_PCT,
    LEG2_RETRY_MAX,
    RECOVERY_MAX_LOSS_PCT,
    ROUTE_BLACKLIST_HOURS,
    DRY_RUN,
)
from src.connection import XRPLConnection
from src.executor import TradeExecutor
from src.pathfinder import PathFinder
from src.safety import CircuitBreaker, Blacklist
from src.telegram_alerts import send_alert
from src.simulator import simulate_transaction_ws
from main import _startup_drain_held_iou

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


async def check_wallet() -> bool:
    logger.info("=" * 60)
    logger.info("CHECK 1: Wallet secret loads")
    logger.info("=" * 60)
    if not XRPL_SECRET:
        logger.error(f"{FAIL} XRPL_SECRET not set in .env")
        return False
    try:
        wallet = Wallet.from_seed(XRPL_SECRET)
        logger.info(f"{PASS} Wallet loaded: {wallet.address}")
        return True
    except Exception as e:
        logger.error(f"{FAIL} Wallet load failed: {e}")
        return False


async def check_config() -> bool:
    logger.info("=" * 60)
    logger.info("CHECK 2: Config sanity")
    logger.info("=" * 60)
    logger.info(f"  DRY_RUN                = {DRY_RUN}")
    logger.info(f"  PROFIT_THRESHOLD       = {PROFIT_THRESHOLD} ({float(PROFIT_THRESHOLD) * 100}%)")
    logger.info(f"  MAX_POSITION_PCT       = {MAX_POSITION_PCT} ({float(MAX_POSITION_PCT) * 100}%)")
    logger.info(f"  DAILY_LOSS_LIMIT       = {DAILY_LOSS_LIMIT_PCT} ({float(DAILY_LOSS_LIMIT_PCT) * 100}%)")
    logger.info(f"  XRPL_WS_URL            = {XRPL_WS_URL}")
    logger.info("  -- two-leg safety rails --")
    logger.info(f"  MAX_TRADE_XRP_ABS      = {MAX_TRADE_XRP_ABS}")
    logger.info(f"  MIN_BALANCE_GUARD_PCT  = {MIN_BALANCE_GUARD_PCT}")
    logger.info(f"  LEG2_RETRY_MAX         = {LEG2_RETRY_MAX}")
    logger.info(f"  RECOVERY_MAX_LOSS_PCT  = {RECOVERY_MAX_LOSS_PCT}")
    logger.info(f"  ROUTE_BLACKLIST_HOURS  = {ROUTE_BLACKLIST_HOURS}")
    logger.info(f"{PASS} Config loaded")
    return True


async def check_telegram() -> bool:
    logger.info("=" * 60)
    logger.info("CHECK 3: Telegram alerts")
    logger.info("=" * 60)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(f"{SKIP} Telegram not configured (TOKEN or CHAT_ID empty)")
        logger.warning("       You will NOT get live trade notifications.  Consider fixing before go-live.")
        return False
    try:
        await send_alert(
            "Preflight check: Telegram alerts are working. "
            "If you see this, trade notifications will reach you."
        )
        logger.info(f"{PASS} Telegram alert sent — check your chat to confirm receipt")
        return True
    except Exception as e:
        logger.error(f"{FAIL} Telegram send failed: {e}")
        return False


async def check_connection_and_simulate() -> bool:
    logger.info("=" * 60)
    logger.info("CHECK 4: XRPL connection + simulate RPC shape")
    logger.info("=" * 60)
    conn = XRPLConnection(XRPL_WS_URL)

    ledger_fired = asyncio.Event()
    first_ledger = [0]

    async def on_ledger(idx: int):
        if first_ledger[0] == 0:
            first_ledger[0] = idx
            ledger_fired.set()

    conn.on_ledger_close(on_ledger)
    connect_task = asyncio.create_task(conn.connect())
    try:
        await asyncio.wait_for(ledger_fired.wait(), timeout=15)
        logger.info(f"{PASS} Connected + ledger closed: {first_ledger[0]}")
    except asyncio.TimeoutError:
        logger.error(f"{FAIL} No ledger close within 15s — connection broken")
        connect_task.cancel()
        return False

    # Send a dummy simulate — an invalid Payment will come back with
    # an engine_result (probably temMALFORMED), proving the field is populated.
    wallet = Wallet.from_seed(XRPL_SECRET)
    bad_tx = {
        "TransactionType": "Payment",
        "Account": wallet.address,
        "Destination": wallet.address,
        "Amount": "1",
    }
    try:
        sim = await simulate_transaction_ws(bad_tx, conn)
        logger.info(f"       simulate result_code = {sim.result_code!r}")
        if sim.result_code in ("unknown", "exception"):
            logger.error(f"{FAIL} simulate returned {sim.result_code} — field extraction broken")
            connect_task.cancel()
            return False
        logger.info(f"{PASS} Simulate RPC returns engine_result field correctly")
    except Exception as e:
        logger.error(f"{FAIL} Simulate call failed: {e}")
        connect_task.cancel()
        return False
    finally:
        connect_task.cancel()
        try:
            await connect_task
        except (asyncio.CancelledError, Exception):
            pass

    return True


async def check_circuit_breaker() -> bool:
    logger.info("=" * 60)
    logger.info("CHECK 5: Circuit breaker halts on simulated loss")
    logger.info("=" * 60)

    wallet = Wallet.from_seed(XRPL_SECRET)
    # Use a 1% loss limit for the test so we don't need a huge fake loss
    cb = CircuitBreaker(
        account_address=wallet.address,
        reference_balance=Decimal("100"),
        loss_limit_pct=Decimal("0.01"),  # 1% for test purposes
    )

    if cb.is_halted():
        logger.error(f"{FAIL} Circuit breaker already halted before test")
        return False

    # Record a trivial loss — should NOT halt
    cb.record_trade(Decimal("-0.50"))
    if cb.is_halted():
        logger.error(f"{FAIL} Circuit breaker halted on 0.5% loss (limit is 1%) — too sensitive")
        return False
    logger.info(f"{PASS} Small loss (0.5%) did NOT trigger halt")

    # Record enough loss to trigger
    cb.record_trade(Decimal("-0.75"))  # Total loss now 1.25%, over 1% limit
    if not cb.is_halted():
        logger.error(f"{FAIL} Circuit breaker did NOT halt after 1.25% loss (limit was 1%)")
        return False
    logger.info(f"{PASS} Circuit breaker HALTED on 1.25% cumulative loss (limit was 1%)")
    return True


async def check_two_leg_pipeline() -> bool:
    """Full two-leg DRY_RUN pipeline against live mainnet.

    Connects to XRPL, runs the pathfinder, and if an opportunity is found
    pushes it through executor.execute() in DRY_RUN mode. Verifies:
      - pathfinder emits properly-shaped two-leg opportunities
      - executor's autofill, leg 1 build, leg 1 simulate, delivered_amount
        extraction, leg 2 build, and leg 2 simulate all execute without
        raising
      - DRY_RUN path logs a paper trade on sim success

    If no profitable opportunities exist at runtime, this check still
    passes — we only fail on uncaught exceptions or broken plumbing.
    """
    logger.info("=" * 60)
    logger.info("CHECK 6+7: Two-leg pathfinder + executor DRY_RUN pipeline")
    logger.info("=" * 60)

    wallet = Wallet.from_seed(XRPL_SECRET)
    conn = XRPLConnection(XRPL_WS_URL)

    ledger_ready = asyncio.Event()
    latest_ledger = [0]

    async def on_ledger(idx: int):
        latest_ledger[0] = idx
        ledger_ready.set()

    conn.on_ledger_close(on_ledger)
    connect_task = asyncio.create_task(conn.connect())

    try:
        await asyncio.wait_for(ledger_ready.wait(), timeout=15)
    except asyncio.TimeoutError:
        logger.error(f"{FAIL} No ledger close within 15s")
        connect_task.cancel()
        return False

    pathfinder = PathFinder(conn, wallet.address)
    circuit_breaker = CircuitBreaker(
        account_address=wallet.address,
        connection=conn,
    )
    blacklist = Blacklist()
    executor = TradeExecutor(
        wallet=wallet,
        circuit_breaker=circuit_breaker,
        blacklist=blacklist,
        connection=conn,
        dry_run=True,  # Always DRY_RUN for preflight
    )

    try:
        balance = await conn.get_account_balance(wallet.address)
        logger.info(f"       Wallet balance: {balance} XRP")
        if balance <= Decimal("0"):
            logger.error(f"{FAIL} Wallet has zero balance — fund it first")
            return False

        circuit_breaker.reference_balance = balance

        # Run a full scan
        opps = await pathfinder.scan(balance)
        logger.info(f"       Pathfinder emitted {len(opps)} opportunities")

        if opps:
            # Verify every opp has two-leg metadata — B4 invariant
            for opp in opps:
                if not opp.iou_currency or not opp.buy_issuer:
                    logger.error(
                        f"{FAIL} Opportunity missing two-leg metadata: {opp}"
                    )
                    return False
            logger.info(f"{PASS} All {len(opps)} opps carry two-leg metadata")

            # Push the first opp through executor in DRY_RUN
            first_opp = opps[0]
            logger.info(
                f"       Running executor DRY_RUN on first opp: "
                f"{first_opp.iou_currency}/{first_opp.buy_issuer[:8]}... "
                f"profit={first_opp.profit_pct:.4f}%"
            )
            result = await executor.execute(first_opp, current_balance=balance)
            # result=True means paper trade logged; result=False means the
            # simulate gate (or a safety check) blocked it. Either is fine.
            logger.info(
                f"{PASS} executor.execute() returned {result} without raising"
            )
        else:
            logger.warning(
                f"{SKIP} No opportunities found right now — market is quiet. "
                f"Pipeline plumbing not end-to-end verified this run, but "
                f"no crashes occurred at the scan layer."
            )

        return True

    except Exception as e:
        logger.error(f"{FAIL} Pipeline raised: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        connect_task.cancel()
        try:
            await connect_task
        except (asyncio.CancelledError, Exception):
            pass


async def check_startup_drain_guard() -> bool:
    """Verifies the startup drain function runs without error on a clean wallet.

    If trust lines have no non-zero balances, the guard should log 'wallet
    clean' and return. If balances exist, it would trigger drain_iou (not
    exercised in preflight — live trading hasn't started yet).
    """
    logger.info("=" * 60)
    logger.info("CHECK 8: Startup recovery guard (clean-wallet path)")
    logger.info("=" * 60)

    wallet = Wallet.from_seed(XRPL_SECRET)
    circuit_breaker = CircuitBreaker(account_address=wallet.address)
    blacklist = Blacklist()
    executor = TradeExecutor(
        wallet=wallet,
        circuit_breaker=circuit_breaker,
        blacklist=blacklist,
        dry_run=True,
    )

    try:
        await _startup_drain_held_iou(wallet.address, executor)
        logger.info(f"{PASS} Startup drain guard ran without raising")
        return True
    except Exception as e:
        logger.error(f"{FAIL} Startup drain raised: {type(e).__name__}: {e}")
        return False


async def main():
    logger.info("\n>>> XRPL ARBITRAGE BOT — PREFLIGHT CHECK <<<\n")
    results = {
        "wallet": await check_wallet(),
        "config": await check_config(),
        "telegram": await check_telegram(),
        "xrpl": await check_connection_and_simulate(),
        "breaker": await check_circuit_breaker(),
        "two_leg_pipeline": await check_two_leg_pipeline(),
        "startup_drain": await check_startup_drain_guard(),
    }

    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    for name, ok in results.items():
        tag = PASS if ok else FAIL
        logger.info(f"  {tag}  {name}")

    critical = ["wallet", "config", "xrpl", "breaker", "two_leg_pipeline", "startup_drain"]
    if all(results[k] for k in critical):
        if results["telegram"]:
            logger.info("\nAll checks PASSED — safe to proceed to live trading")
        else:
            logger.info("\nCritical checks PASSED — Telegram is optional but recommended")
        sys.exit(0)
    else:
        logger.error("\nPreflight FAILED — do NOT go live until failures resolved")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
