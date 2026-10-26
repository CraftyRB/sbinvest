"""Background market-intelligence daemon.

Continuously polls every ingestion feed, runs the risk math and the adversarial
Cerebras debate, then writes state straight into `investments.db`.

Usage:
    python daemon.py                 # loop forever (APScheduler, every 30 min)
    python daemon.py --once          # single cycle, then exit
    python daemon.py --interval 10   # custom interval in minutes
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Explicit load: all entry points read secrets from env.txt.
load_dotenv("env.txt", override=False)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database  # noqa: E402
import pipeline  # noqa: E402
from intelligence import budget  # noqa: E402

LOG = logging.getLogger("skyblock.daemon")


def _configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


async def run_cycle(
    top_n: int,
    commit_limit: int,
    min_weekly_coins: float,
    send_alert: bool,
    lock: asyncio.Lock,
) -> dict | None:
    """One full scan, executed off the event loop (pipeline is synchronous)."""
    if lock.locked():
        LOG.warning("Previous scan still running; skipping this tick.")
        return None

    async with lock:
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        database.set_setting("daemon_status", "scanning")
        database.set_setting("daemon_last_start", database.utcnow())
        LOG.info("Cycle start: bazaar + 7 alt feeds -> confluence -> debate")
        try:
            result = await loop.run_in_executor(
                None,
                lambda: pipeline.run_scan(
                    top_n=top_n,
                    commit_limit=commit_limit,
                    min_weekly_coins=min_weekly_coins,
                    send_alert=send_alert,
                    persist=True,
                ),
            )
        except Exception as exc:  # keep the daemon alive on any feed/model error
            LOG.exception("Cycle failed")
            database.set_setting("daemon_status", "error")
            database.set_setting("daemon_last_error", f"{type(exc).__name__}: {exc}"[:500])
            database.set_setting("daemon_last_end", database.utcnow())
            return None

        elapsed = time.perf_counter() - started
        database.set_setting("daemon_status", "idle")
        database.set_setting("daemon_last_end", database.utcnow())
        database.set_setting("daemon_last_error", "")
        database.set_setting(
            "daemon_last_summary",
            f"{result['source']} | {len(result['signals'])} signals | "
            f"{result['stored']} new | {elapsed:.1f}s",
        )
        database.set_setting(
            "daemon_cycles", str(int(database.get_setting("daemon_cycles", "0") or 0) + 1)
        )
        LOG.info(
            "Cycle done in %.1fs: %s signals (%s), %s stored, threshold %.3f",
            elapsed,
            len(result["signals"]),
            result["source"],
            result["stored"],
            result["threshold"]["buy_threshold"],
        )
        for warning in result.get("warnings") or []:
            LOG.warning("feed: %s", warning)
        return result


async def run_forever(args: argparse.Namespace) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    lock = asyncio.Lock()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle,
        trigger=IntervalTrigger(minutes=args.interval),
        id="skyblock_scan",
        max_instances=1,
        coalesce=True,
        kwargs={
            "top_n": args.top,
            "commit_limit": args.commits,
            "min_weekly_coins": args.min_volume_m * 1_000_000,
            "send_alert": args.alert,
            "lock": lock,
        },
        next_run_time=None,
    )
    scheduler.start()
    database.set_setting("daemon_status", "starting")
    database.set_setting("daemon_interval", str(args.interval))
    LOG.info("Daemon online. Interval=%s min. Ctrl+C to stop.", args.interval)

    # Prime the desk immediately, then let the scheduler take over.
    await run_cycle(
        top_n=args.top,
        commit_limit=args.commits,
        min_weekly_coins=args.min_volume_m * 1_000_000,
        send_alert=args.alert,
        lock=lock,
    )
    scheduler.reschedule_job(
        "skyblock_scan", trigger=IntervalTrigger(minutes=args.interval)
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        database.set_setting("daemon_status", "stopped")
        LOG.info("Daemon stopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description="SkyBlock market intelligence daemon")
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    parser.add_argument("--interval", type=int, default=config.SCAN_INTERVAL_MINUTES)
    parser.add_argument("--top", type=int, default=config.DAEMON_TOP_N)
    parser.add_argument("--commits", type=int, default=3)
    parser.add_argument("--min-volume-m", type=float, default=0.25)
    parser.add_argument("--alert", action="store_true", help="push new signals to Discord")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _configure_logging(args.verbose)
    database.init_db()
    LOG.info(
        "DB=%s model=%s base=%s interval=%smin",
        config.DB_PATH.name,
        budget.model(),
        config.CEREBRAS_BASE_URL,
        args.interval,
    )

    if args.once:
        result = asyncio.run(
            run_cycle(
                top_n=args.top,
                commit_limit=args.commits,
                min_weekly_coins=args.min_volume_m * 1_000_000,
                send_alert=args.alert,
                lock=asyncio.Lock(),
            )
        )
        return 0 if result else 1

    try:
        asyncio.run(run_forever(args))
    except KeyboardInterrupt:
        LOG.info("Interrupted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
