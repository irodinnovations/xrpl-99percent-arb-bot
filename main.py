"""XRPL 99%+ Arbitrage Bot -- main entry point.

Connects to XRPL mainnet, subscribes to ledger + book_changes streams,
scans for arbitrage opportunities with parallel RPC, validates via
simulate RPC, and either paper-trades or live-executes.

Scanning architecture:
- Event-driven (fast path): book_changes fires every ledger close (~4-7s),
  triggers scan_pairs() on only the currencies whose rates changed.
- Periodic (fallback): full scan of all 27 IOUs + multi-hop discovery via
  ripple_path_find, every SCAN_INTERVAL ledgers.

Note: the transactions stream is NOT subscribed because XRPL mainnet
pushes hundreds of txns per ledger, flooding the WebSocket message queue.
AMM mispricings are detected through book_changes rate shifts instead.

Safety rules enforced here:
- Bot never starts without XRPL_SECRET set (T-01-10)
- DRY_RUN defaults to True in config -- explicit .env change required (DRY-04)
- Top-level exception handler prevents main loop crash (T-01-11)
- Circuit breaker checked every scan cycle -- halts on daily loss limit (SAFE-02)
- asyncio.Lock prevents overlapping scans across all triggers
"""

import asyncio
import logging
import time as _time
from decimal import Decimal

from xrpl.wallet import Wallet

from src.config import (
    XRPL_SECRET,
    DRY_RUN,
    LOG_LEVEL,
    PROFIT_THRESHOLD,
    MAX_POSITION_PCT,
    SCAN_INTERVAL,
    VOLATILITY_WINDOW,
)
from src.connection import XRPLConnection
from src.pathfinder import PathFinder
from src.executor import TradeExecutor
from src.safety import CircuitBreaker, Blacklist
from src.trade_logger import setup_logging
from src.telegram_alerts import send_alert
from src.ai_brain import review_trade
from src.volatility import VolatilityTracker

logger = logging.getLogger(__name__)


async def _execute_opportunities(
    opportunities: list,
    executor: TradeExecutor,
    dry_run: bool,
    trigger: str = "",
) -> None:
    """Execute opportunities and fire-and-forget AI reviews.

    Shared by all scan triggers (ledger close, book_changes).
    """
    for opp in opportunities:
        label = f"{trigger} " if trigger else ""
        logger.info(
            f"{label}Opportunity: {opp.profit_pct:.4f}% profit | "
            f"In: {opp.input_xrp} XRP -> Out: {opp.output_xrp} XRP"
        )
        result = await executor.execute(opp)
        if result:
            trade_review_data = {
                "profit_pct": str(opp.profit_pct),
                "profit_ratio": str(opp.profit_ratio),
                "input_xrp": str(opp.input_xrp),
                "output_xrp": str(opp.output_xrp),
                "dry_run": dry_run,
            }
            if trigger:
                trade_review_data["trigger"] = trigger
            asyncio.create_task(review_trade(trade_review_data))


async def main():
    """Main bot loop.

    Initializes all modules, registers stream callbacks, and
    connects to XRPL. The connection loop runs forever with auto-reconnect.
    """
    # Setup logging first so all subsequent logs are formatted
    setup_logging()

    # Validate wallet secret before doing anything else (T-01-10)
    if not XRPL_SECRET:
        logger.error("XRPL_SECRET not set in .env -- cannot start bot")
        return

    wallet = Wallet.from_seed(XRPL_SECRET)
    logger.info(f"Wallet address: {wallet.address}")
    logger.info(f"Mode: {'DRY RUN (paper trading)' if DRY_RUN else 'LIVE TRADING'}")
    logger.info(f"Profit threshold: {PROFIT_THRESHOLD * 100}%")
    logger.info(f"Max position: {MAX_POSITION_PCT * 100}% of balance")
    logger.info(f"Scan interval: every {SCAN_INTERVAL} ledgers (full scan)")

    # Initialize all modules
    connection = XRPLConnection()
    pathfinder = PathFinder(connection, wallet.address)
    circuit_breaker = CircuitBreaker(
        account_address=wallet.address,
        connection=connection,
    )
    blacklist = Blacklist()
    executor = TradeExecutor(
        wallet=wallet,
        circuit_breaker=circuit_breaker,
        blacklist=blacklist,
        connection=connection,
        dry_run=DRY_RUN,
    )

    # Initialize optimization modules
    volatility_tracker = VolatilityTracker(window_seconds=VOLATILITY_WINDOW)

    logger.info(f"Volatility window: {VOLATILITY_WINDOW}s")

    # Track ledger count; asyncio.Lock prevents overlapping scans
    ledger_count = 0
    scan_count = 0
    scan_lock = asyncio.Lock()

    # Timestamp of last book_changes for change detection
    last_book_changes_ts = [0.0]

    async def on_ledger_close(ledger_index: int):
        """Periodic full scan every SCAN_INTERVAL ledgers (~30-55s).

        This is the fallback scan that covers all 27 IOUs plus multi-hop
        discovery via ripple_path_find.  The event-driven scan_pairs()
        via on_book_changes handles the fast path (~4-7s).
        """
        nonlocal ledger_count, scan_count
        ledger_count += 1

        if circuit_breaker.is_halted():
            if ledger_count % 100 == 0:
                logger.info("Circuit breaker active -- skipping scans")
            return

        if ledger_count % SCAN_INTERVAL != 0:
            return

        if scan_lock.locked():
            logger.debug("Scan lock held -- skipping full scan interval")
            return

        async with scan_lock:
            scan_count += 1
            try:
                balance = await connection.get_account_balance(wallet.address)
                if balance <= Decimal("0"):
                    logger.warning("Zero balance -- skipping scan")
                    return

                if circuit_breaker.reference_balance <= Decimal("0"):
                    circuit_breaker.reference_balance = balance
                    logger.info(f"Circuit breaker reference balance set: {balance} XRP")

                opportunities = await pathfinder.scan(
                    balance,
                    volatility_tracker=volatility_tracker,
                )

                await _execute_opportunities(opportunities, executor, DRY_RUN)

                if scan_count % 5 == 0:
                    global_vol = volatility_tracker.get_global_volatility()
                    logger.info(
                        f"Heartbeat: ledger={ledger_index}, scans={scan_count}, "
                        f"balance={balance} XRP, volatility={global_vol:.4f}, "
                        f"halted={circuit_breaker.is_halted()}"
                    )

            except Exception as e:
                logger.error(f"Scan error at ledger {ledger_index}: {e}")

    async def on_book_changes(message: dict):
        """Event-driven hot path: scan changed pairs every ledger close (~4-7s).

        1. Always feed the volatility tracker (even if scan_lock is held)
        2. Identify which currencies had rate changes
        3. Trigger targeted scan on ONLY those currencies
        """
        try:
            # Always feed volatility tracker -- this is cheap and non-blocking
            before_ts = last_book_changes_ts[0]
            volatility_tracker.process_book_changes_message(message)
            last_book_changes_ts[0] = _time.time()

            if circuit_breaker.is_halted():
                return

            changed = volatility_tracker.get_changed_currencies(before_ts)
            if not changed:
                return

            if scan_lock.locked():
                return

            async with scan_lock:
                balance = await connection.get_account_balance(wallet.address)
                if balance <= Decimal("0"):
                    return

                if circuit_breaker.reference_balance <= Decimal("0"):
                    circuit_breaker.reference_balance = balance

                opportunities = await pathfinder.scan_pairs(
                    changed_currencies=changed,
                    account_balance=balance,
                    volatility_tracker=volatility_tracker,
                )

                await _execute_opportunities(
                    opportunities, executor, DRY_RUN, trigger="book_changes"
                )

        except Exception as e:
            logger.error(f"Book changes processing error: {e}")

    # Register stream callbacks before connecting
    connection.on_ledger_close(on_ledger_close)
    connection.on_book_changes(on_book_changes)

    # Send startup alert (gracefully skipped if Telegram not configured)
    await send_alert(
        f"Bot started | Mode: {'DRY RUN' if DRY_RUN else 'LIVE'} | "
        f"Address: {wallet.address} | "
        f"Streams: ledger + book_changes"
    )

    logger.info("Starting XRPL connection...")
    # connect() runs forever -- auto-reconnects on disconnect
    await connection.connect()


if __name__ == "__main__":
    asyncio.run(main())
