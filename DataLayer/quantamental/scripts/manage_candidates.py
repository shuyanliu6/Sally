"""
Manage the candidate trading list (config/candidate_list.json).

The candidate list is the subset of the research universe you actively consider
for trading. It's grouped by sector so different signal logic can apply per
sector (e.g. AI capex signals for upstream_compute, commodity/utility signals
for power_cooling).

If candidate_list.json doesn't exist, the system uses BASE_CANDIDATES (the seed
sectors from config/universe.py). The first --add/--remove/--set creates it.

Usage:
    python scripts/manage_candidates.py --show
    python scripts/manage_candidates.py --add CRWD PANW --sector application_architecture --note "Cybersecurity Q2"
    python scripts/manage_candidates.py --remove BABA --note "Regulatory overhang"
    python scripts/manage_candidates.py --set NVDA TSM AVGO --sector upstream_compute --note "Trim semis"
    python scripts/manage_candidates.py --reset

Sectors are free-form strings — pass any name you want. The seed sectors are:
    upstream_compute, cloud_infrastructure, power_cooling,
    application_architecture, networking, non_us, benchmarks
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.universe import (
    BASE_CANDIDATES,
    BASE_CANDIDATE_TICKERS,
    UNCATEGORIZED_SECTOR,
    candidate_list_metadata,
    candidate_list_source,
    load_candidate_list,
    load_candidate_list_by_sector,
    reset_candidate_list,
    save_candidate_list,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("candidates")

CANDIDATE_FILE = Path(__file__).parent.parent / "config" / "candidate_list.json"


# ── ANSI ──────────────────────────────────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ticker_to_sector(grouped: dict[str, list[str]]) -> dict[str, str]:
    """Reverse-index: ticker → sector it currently sits in."""
    return {t: sector for sector, tickers in grouped.items() for t in tickers}


def _format_grouped(grouped: dict[str, list[str]]) -> str:
    lines = []
    total = 0
    for sector, tickers in grouped.items():
        total += len(tickers)
        lines.append(f"  {BOLD}{sector}{RESET} ({len(tickers)}): {', '.join(tickers)}")
    return "\n".join(lines), total


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_show():
    grouped = load_candidate_list_by_sector()
    source = candidate_list_source()
    body, total = _format_grouped(grouped)

    print(f"\n{BOLD}Candidate List{RESET}  ({total} tickers across {len(grouped)} sectors)")
    print(f"{DIM}Source: {source}{RESET}\n")
    print(body)

    data = candidate_list_metadata()
    note_text = data.get("notes") or data.get("note") or ""
    if note_text:
        print(f"\n{DIM}Latest note: {note_text}")
        print(f"Updated: {data.get('updated_at', 'unknown')}{RESET}")
    print()


def cmd_add(tickers_to_add: list[str], sector: str | None, note: str | None):
    target_sector = sector or UNCATEGORIZED_SECTOR
    grouped = load_candidate_list_by_sector()
    upper = [t.upper() for t in tickers_to_add]

    # Move-to-target semantics: if a ticker is already in another sector, move it.
    current_index = _ticker_to_sector(grouped)
    moved = []
    added = []
    no_op = []
    for t in upper:
        existing = current_index.get(t)
        if existing == target_sector:
            no_op.append(t)
        elif existing is not None:
            grouped[existing] = [x for x in grouped[existing] if x != t]
            grouped.setdefault(target_sector, []).append(t)
            moved.append((t, existing))
        else:
            grouped.setdefault(target_sector, []).append(t)
            added.append(t)

    if not added and not moved:
        print(f"{YELLOW}All tickers already in sector '{target_sector}'.{RESET}")
        return

    save_candidate_list(grouped, note=note or "")
    if added:
        print(f"{GREEN}✅ Added to '{target_sector}': {', '.join(added)}{RESET}")
    if moved:
        msg = ", ".join(f"{t} (from {old})" for t, old in moved)
        print(f"{GREEN}✅ Moved to '{target_sector}': {msg}{RESET}")
    if no_op:
        print(f"{DIM}   Already in '{target_sector}': {', '.join(no_op)}{RESET}")


def cmd_remove(tickers_to_remove: list[str], note: str | None):
    grouped = load_candidate_list_by_sector()
    current_index = _ticker_to_sector(grouped)
    upper = [t.upper() for t in tickers_to_remove]
    removed = []
    not_found = []
    for t in upper:
        sector = current_index.get(t)
        if sector is None:
            not_found.append(t)
            continue
        grouped[sector] = [x for x in grouped[sector] if x != t]
        removed.append((t, sector))

    if not removed:
        print(f"{YELLOW}None of those tickers are in the candidate list.{RESET}")
        return

    # Drop now-empty sectors so the JSON stays clean
    grouped = {s: t for s, t in grouped.items() if t}
    save_candidate_list(grouped, note=note or "")
    msg = ", ".join(f"{t} (from {sec})" for t, sec in removed)
    print(f"{GREEN}✅ Removed: {msg}{RESET}")
    if not_found:
        print(f"{YELLOW}   Not in list (skipped): {', '.join(not_found)}{RESET}")


def cmd_set(tickers: list[str], sector: str | None, note: str | None):
    """--set behaviour:
       - with --sector: replace ONLY that sector (other sectors untouched)
       - without --sector: replace the entire list (single uncategorized bucket)
    """
    upper = sorted({t.upper() for t in tickers})
    if sector:
        grouped = load_candidate_list_by_sector()
        # Strip these tickers from every other sector first (no duplicates)
        for other_sector in list(grouped.keys()):
            if other_sector != sector:
                grouped[other_sector] = [t for t in grouped[other_sector] if t not in upper]
        grouped[sector] = upper
        grouped = {s: t for s, t in grouped.items() if t}
        save_candidate_list(grouped, note=note or "")
        print(f"{GREEN}✅ Sector '{sector}' replaced with {len(upper)} tickers{RESET}")
        print(f"   {', '.join(upper)}")
    else:
        # Wipe everything, save as a single uncategorized bucket
        save_candidate_list(upper, note=note or "")
        print(f"{GREEN}✅ Candidate list replaced with {len(upper)} tickers "
              f"(all in '{UNCATEGORIZED_SECTOR}' — re-organise via --sector adds){RESET}")
        print(f"   {', '.join(upper)}")


def cmd_reset():
    if not CANDIDATE_FILE.exists():
        print(f"{YELLOW}Already using BASE_CANDIDATES (no JSON file present).{RESET}")
        return
    reset_candidate_list()
    print(f"{GREEN}✅ Reset to BASE_CANDIDATES "
          f"({len(BASE_CANDIDATE_TICKERS)} tickers across {len(BASE_CANDIDATES)} sectors){RESET}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Manage the candidate trading list (sector-aware)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Seed sectors: " + ", ".join(BASE_CANDIDATES.keys()) + "\n"
            "Custom sector names also accepted — pass any string with --sector."
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show",   action="store_true",   help="Print the current candidate list (grouped)")
    g.add_argument("--add",    nargs="+", metavar="T", help="Add ticker(s) to a sector (default: uncategorized)")
    g.add_argument("--remove", nargs="+", metavar="T", help="Remove ticker(s) from whichever sector they're in")
    g.add_argument("--set",    nargs="+", metavar="T", dest="set_list",
                   help="Replace tickers — only that sector if --sector given, else entire list")
    g.add_argument("--reset",  action="store_true",   help="Delete JSON, fall back to BASE_CANDIDATES")

    p.add_argument("--sector", help="Sector name for --add / --set "
                                    f"(default for --add: {UNCATEGORIZED_SECTOR})")
    p.add_argument("--note",   help="Note explaining the change (saved to JSON)")

    args = p.parse_args()

    if args.show:
        cmd_show()
    elif args.add:
        cmd_add(args.add, args.sector, args.note)
    elif args.remove:
        cmd_remove(args.remove, args.note)
    elif args.set_list:
        cmd_set(args.set_list, args.sector, args.note)
    elif args.reset:
        cmd_reset()


if __name__ == "__main__":
    main()
