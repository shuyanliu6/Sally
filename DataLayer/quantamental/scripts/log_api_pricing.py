"""
Manual entry CLI for AI API pricing (Signal D).

Track inference pricing across major API providers (OpenAI, Anthropic, Google).
Signal D scores the trajectory: rising = +1, stable/normal-decay = 0,
big drops = -1 or -2 (DeepSeek-style efficiency shocks).

Check pricing weekly. Log new entries when any provider changes prices.

Usage:
    python scripts/log_api_pricing.py --date 2026-04-26 --provider OpenAI --model gpt-5 --in 5.0 --out 15.0
    python scripts/log_api_pricing.py --show
    python scripts/log_api_pricing.py --score
"""

import argparse
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import SQLITE_PATH
from signals.sector_ai_infra import (
    add_api_pricing,
    init_ai_infra_db,
    latest_api_pricing_signal,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def cmd_show(path: str = SQLITE_PATH):
    init_ai_infra_db(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM api_pricing ORDER BY date DESC, provider, model LIMIT 50"
    ).fetchall()
    con.close()

    if not rows:
        print("No API pricing data yet. Add with --date --provider --model --in --out.")
        return

    print(f"\n{'Date':<12} {'Provider':<10} {'Model':<25} {'In ($/M)':>10} {'Out ($/M)':>10}")
    print("─" * 75)
    for r in rows:
        print(f"{r['date']:<12} {r['provider']:<10} {r['model']:<25} "
              f"{r['price_per_m_input']:>10.2f} {r['price_per_m_output']:>10.2f}")
    print()


def cmd_score():
    score = latest_api_pricing_signal()
    print(f"\nSignal D (AI API pricing trend): {score:+d}")
    print("   +1: rising/stable    0: normal decay (<30%/qtr)")
    print("   -1: falling 30-50%/qtr   -2: falling >50%/qtr (DeepSeek-style shock)")


def main():
    p = argparse.ArgumentParser(description="Log AI API pricing (Signal D)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show",  action="store_true", help="Print recent pricing data")
    g.add_argument("--score", action="store_true", help="Compute current Signal D")
    g.add_argument("--date",  help="Pricing observation date YYYY-MM-DD")

    p.add_argument("--provider", help="OpenAI | Anthropic | Google | etc.")
    p.add_argument("--model",    help="Model name, e.g. gpt-5, claude-4-opus")
    p.add_argument("--in",       dest="price_in",  type=float,
                   help="$/million input tokens")
    p.add_argument("--out",      dest="price_out", type=float,
                   help="$/million output tokens")

    args = p.parse_args()

    if args.show:
        cmd_show()
        return
    if args.score:
        cmd_score()
        return

    if not all([args.provider, args.model, args.price_in is not None, args.price_out is not None]):
        p.error("--provider, --model, --in, --out all required when --date is set")

    add_api_pricing(args.date, args.provider, args.model, args.price_in, args.price_out)
    print(f"\n✅ Logged {args.provider}/{args.model} pricing for {args.date}")
    cmd_score()


if __name__ == "__main__":
    main()
