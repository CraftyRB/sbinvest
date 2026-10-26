"""Headless scan CLI.

    python scripts/scan.py            # scan, persist, no Discord alert
    python scripts/scan.py --alert    # also push to the Discord webhook
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv("env.txt", override=False)

import database  # noqa: E402
import pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--commits", type=int, default=3)
    parser.add_argument("--min-volume-m", type=float, default=0.25)
    parser.add_argument("--alert", action="store_true")
    args = parser.parse_args()

    database.init_db()
    result = pipeline.run_scan(
        top_n=args.top,
        commit_limit=args.commits,
        min_weekly_coins=args.min_volume_m * 1_000_000,
        send_alert=args.alert,
    )

    print(f"Scan {result['scanned_at']} | {result['products']:,} products | source={result['source']}")
    print(f"Threshold {result['threshold']['buy_threshold']:.3f} | win-rate {result['stats']['win_rate']:.0%}")
    print(f"Market view: {result['market_view']}")
    for s in result["signals"]:
        print(
            f"  {s['signal']:<4} {s['item_id']:<28} conf={s['confidence']:.2f} "
            f"entry={s['entry_price']:,.0f} target={s['target_price']:,.0f} "
            f"stop={s['stop_price']:,.0f} sector={s['sector']}"
        )
    print(f"Stored {result['stored']} new signals, alert_sent={result['alert_sent']}")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
