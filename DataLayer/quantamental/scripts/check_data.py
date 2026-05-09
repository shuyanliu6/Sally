"""
Data health check — run after the pipeline to verify everything landed correctly.

Usage:
    python scripts/check_data.py
    python scripts/check_data.py --days 30   # check last 30 trading days

Prints a colour-coded report for:
  - OHLCV coverage per ticker (rows, date range, latest price, any nulls)
  - Macro indicator coverage (rows, latest date, latest value, expected range)
  - Regime signals (latest classification, score, history length)
  - Overall pass / warn / fail summary
"""

import os, sys
from datetime import date, timedelta

if __package__ in (None, ""):
    from _bootstrap import add_project_root
    add_project_root(__file__)

from quantamental.config.universe import FRED_SERIES, load_candidate_list, load_research_universe
from quantamental.data.ingest.questdb_connection import symbol_list_clause

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    return f"{GREEN}✅ {msg}{RESET}"
def warn(msg):  return f"{YELLOW}⚠️  {msg}{RESET}"
def fail(msg):  return f"{RED}❌ {msg}{RESET}"
def header(msg):return f"\n{BOLD}{msg}{RESET}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def prev_trading_day(from_date=None):
    d = (from_date or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def trading_days_in_range(start: date, end: date) -> int:
    """Approximate count of weekdays between start and end (inclusive)."""
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# ── QuestDB connection ────────────────────────────────────────────────────────

def get_writer():
    try:
        from quantamental.data.ingest import questdb_writer
        questdb_writer.query("SELECT 1")
        return questdb_writer
    except Exception as e:
        print(fail(f"Cannot connect to QuestDB: {e}"))
        print("  → Make sure Docker is running:  docker compose up -d")
        sys.exit(1)


# ── Section 1: OHLCV coverage ─────────────────────────────────────────────────

def check_ohlcv(writer, lookback_days: int):
    print(header("━━━ 1. OHLCV Coverage (daily_ohlcv) ━━━"))

    end_date   = prev_trading_day()
    start_date = end_date - timedelta(days=lookback_days * 1.5)  # generous window
    expected_days = trading_days_in_range(start_date, end_date)
    expected_days = min(expected_days, lookback_days)

    candidates = sorted(set(load_candidate_list()))
    research = sorted(set(load_research_universe()))
    active_symbols = sorted(set(candidates) | set(research))
    candidate_set = set(candidates)
    research_only = set(research) - candidate_set

    print(
        f"Active universe: {len(candidates)} candidate tickers"
        f" + {len(research_only)} research-only tickers"
    )

    clause, params = symbol_list_clause(active_symbols)
    params["start_date"] = str(start_date)

    summary = writer.query("""
        SELECT
            symbol,
            count()            AS rows,
            min(ts)::date      AS first_date,
            max(ts)::date      AS last_date,
            last(close)        AS latest_close,
            sum(CASE WHEN close IS NULL OR close <= 0 THEN 1 ELSE 0 END) AS bad_rows
        FROM daily_ohlcv
        WHERE symbol IN ({symbols})
          AND ts >= :start_date
        ORDER BY symbol
    """.format(symbols=clause), params)

    found_symbols = set(summary["symbol"].tolist()) if not summary.empty else set()
    missing = [t for t in active_symbols if t not in found_symbols]

    issues = []

    if missing:
        print(fail(f"Missing tickers entirely ({len(missing)}): {', '.join(missing)}"))
        issues.append("missing_tickers")
    else:
        print(ok(f"All {len(active_symbols)} active tickers present"))

    if not summary.empty:
        print("\nCandidate rows plus research exceptions:")
        print(f"{'Ticker':<8} {'Scope':<10} {'Rows':>6} {'First':>12} {'Last':>12} {'Latest $':>10} {'Bad':>5}")
        print("─" * 69)
        hidden_research_ok = 0
        for _, row in summary.iterrows():
            sym       = row["symbol"]
            scope     = "candidate" if sym in candidate_set else "research"
            rows      = int(row["rows"])
            last_date = str(row["last_date"])[:10]
            first_date= str(row["first_date"])[:10]
            close     = row["latest_close"]
            bad       = int(row["bad_rows"])

            # Freshness: latest date should be within 5 trading days of expected
            stale = last_date < str(end_date - timedelta(days=7))
            low_rows = rows < max(1, expected_days * 0.8)  # allow 20% gaps

            status = "  "
            show_row = sym in candidate_set or bad > 0 or stale or low_rows
            if bad > 0:
                status = f"{RED}BAD{RESET}"
                issue_cat = "bad_data_candidate" if sym in candidate_set else "bad_data_research"
                issues.append(f"{issue_cat}:{sym}")
            elif stale:
                status = f"{YELLOW}OLD{RESET}"
                issue_cat = "stale_candidate" if sym in candidate_set else "stale_research"
                issues.append(f"{issue_cat}:{sym}")
            elif low_rows:
                status = f"{YELLOW}GAP{RESET}"
                issue_cat = "gaps_candidate" if sym in candidate_set else "gaps_research"
                issues.append(f"{issue_cat}:{sym}")
            else:
                status = f"{GREEN}OK {RESET}"

            if not show_row:
                hidden_research_ok += 1
                continue

            close_str = f"${close:.2f}" if close else "N/A"
            print(
                f"{sym:<8} {scope:<10} {rows:>6} {first_date:>12} {last_date:>12} "
                f"{close_str:>10} {bad:>3}  {status}"
            )
        if hidden_research_ok:
            print(f"... hidden {hidden_research_ok} healthy research-only tickers")

    return issues


# ── Section 2: Macro indicator coverage ───────────────────────────────────────

MACRO_EXPECTED_RANGES = {
    # FRED DGS10: percent (e.g. 4.3 = 4.3%)
    "yield_10y":     (0.0,   10.0,  "% yield",      "daily"),
    # FRED VIXCLS: index points
    "vix":           (5.0,   90.0,  "VIX pts",      "daily"),
    # FRED WALCL: millions of USD (e.g. 6_700_000 = $6.7 trillion)
    "fed_balance":   (3e6,   12e6,  "M USD",        "weekly"),
    # FRED BAMLC0A0CM: percent OAS (e.g. 0.8 = 80bps)
    "credit_spread": (0.2,   5.0,   "% (×100=bps)", "daily"),
}

def check_macro(writer):
    print(header("━━━ 2. Macro Indicators (macro_indicators) ━━━"))

    issues = []
    print(f"\n{'Indicator':<20} {'Rows':>6} {'Latest Date':>14} {'Latest Value':>16} {'In Range':>10}")
    print("─" * 72)

    for name in FRED_SERIES:
        try:
            df = writer.query("""
                SELECT count() AS rows, max(ts)::date AS latest_date, last(value) AS latest_val
                FROM macro_indicators
                WHERE indicator = :indicator
            """, {"indicator": name})
        except Exception as e:
            print(f"{name:<20}  {fail(str(e))}")
            issues.append(f"macro_query_error:{name}")
            continue

        if df.empty or df.iloc[0]["rows"] == 0:
            print(f"{name:<20}  {fail('NO DATA')}")
            issues.append(f"macro_missing:{name}")
            continue

        row        = df.iloc[0]
        rows       = int(row["rows"])
        latest_date= str(row["latest_date"])[:10]
        latest_val = row["latest_val"]

        lo, hi, unit, freq = MACRO_EXPECTED_RANGES.get(name, (None, None, "", ""))
        in_range = lo is not None and lo <= latest_val <= hi

        # Freshness: weekly series can be up to 10 days old
        max_age_days = 10 if freq == "weekly" else 7
        stale = latest_date < str(date.today() - timedelta(days=max_age_days))

        val_str = f"{latest_val:.4f} {unit}" if latest_val else "N/A"

        if not in_range:
            range_status = f"{RED}OUT{RESET}"
            issues.append(f"macro_out_of_range:{name}")
        else:
            range_status = f"{GREEN}OK {RESET}"

        freshness = f"{YELLOW}STALE{RESET}" if stale else f"{GREEN}FRESH{RESET}"
        if stale:
            issues.append(f"macro_stale:{name}")

        print(f"{name:<20} {rows:>6} {latest_date:>14} {val_str:>16}  {range_status}  {freshness}")

    return issues


# ── Section 3: Regime signals ─────────────────────────────────────────────────

def check_signals(writer):
    print(header("━━━ 3. Regime Signals (regime_signals) ━━━"))
    issues = []

    try:
        df = writer.query("""
            SELECT ts, yield_10y_signal, vix_signal, fed_bs_signal,
                   credit_spread_signal, composite_score, regime
            FROM regime_signals
            ORDER BY ts DESC
            LIMIT 5
        """)
    except Exception as e:
        print(fail(f"Cannot query regime_signals: {e}"))
        issues.append("signals_query_error")
        return issues

    if df.empty:
        print(fail("No regime signals found — run:  python scripts/daily_pipeline.py --step calc_signals"))
        issues.append("no_signals")
        return issues

    latest = df.iloc[0]
    regime  = latest["regime"]
    score   = int(latest["composite_score"])
    ts      = str(latest["ts"])[:10]
    stale   = ts < str(date.today() - timedelta(days=3))

    regime_colors = {
        "RISK_ON": GREEN, "MODERATE_ON": GREEN,
        "NEUTRAL": YELLOW,
        "MODERATE_OFF": RED, "RISK_OFF": RED,
    }
    c = regime_colors.get(regime, RESET)

    if stale:
        print(warn(f"Latest signal is from {ts} — may be stale"))
        issues.append("signals_stale")
    else:
        print(ok(f"Latest signal: {ts}"))

    print(f"\n  Regime:  {c}{BOLD}{regime}{RESET}  (composite score: {score:+d})")
    print(f"\n  {'Date':<12} {'Yield':>7} {'VIX':>5} {'Fed':>5} {'Credit':>8} {'Score':>7} {'Regime'}")
    print("  " + "─" * 58)
    for _, row in df.iterrows():
        print(f"  {str(row['ts'])[:10]:<12} "
              f"{int(row['yield_10y_signal']):>+7} "
              f"{int(row['vix_signal']):>+5} "
              f"{int(row['fed_bs_signal']):>+5} "
              f"{int(row['credit_spread_signal']):>+8} "
              f"{int(row['composite_score']):>+7}  "
              f"{row['regime']}")

    if abs(score) > 8:
        print(fail(f"Composite score {score} is outside valid range [-8, +8]"))
        issues.append("score_out_of_range")

    return issues


# ── Section 4: Portfolio state ────────────────────────────────────────────────

def check_portfolio():
    print(header("━━━ 4. Portfolio State (SQLite) ━━━"))
    issues = []
    try:
        from quantamental.portfolio.tracker import get_open_positions
        from quantamental.portfolio.journal import get_recent
        from quantamental.config.settings import SQLITE_PATH

        positions = get_open_positions(SQLITE_PATH)
        journal   = get_recent(n=5, path=SQLITE_PATH)

        if positions.empty:
            print(warn("No open positions — add one with portfolio/tracker.py:add_position()"))
        else:
            print(ok(f"{len(positions)} open position(s):"))
            for _, row in positions.iterrows():
                stop = f"  stop=${row['stop_loss_price']:.2f}" if row.get("stop_loss_price") else ""
                print(f"    {row['symbol']:<6}  {row['shares']} shares @ ${row['entry_price']:.2f}"
                      f"  (entered {row['entry_date']}){stop}")

        if journal.empty:
            print(warn("Trade journal is empty — log your first trade"))
            issues.append("empty_journal")
        else:
            print(ok(f"{len(journal)} recent journal entries"))

    except Exception as e:
        print(fail(f"Portfolio check failed: {e}"))
        issues.append("portfolio_error")

    return issues


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(all_issues: list[str]):
    print(header("━━━ Summary ━━━"))
    if not all_issues:
        print(ok("All checks passed — data looks healthy 🎉"))
    else:
        categories = {}
        for issue in all_issues:
            cat = issue.split(":")[0]
            categories.setdefault(cat, []).append(issue)

        for cat, items in categories.items():
            print(warn(f"{cat} ({len(items)} issue{'s' if len(items)>1 else ''})"))
            for item in items:
                detail = item.split(":", 1)[1] if ":" in item else ""
                if detail:
                    print(f"     → {detail}")

        print(f"\n  Fix suggestions:")
        if any("missing" in i or "stale" in i or "gaps" in i for i in all_issues):
            print("  • Re-run pipeline:     python scripts/daily_pipeline.py --step all --force")
            print("  • Backfill history:    python scripts/backfill.py --start 2024-01-01")
        if any("macro" in i for i in all_issues):
            print("  • Check FRED key:      grep FRED_API_KEY config/.env")
        if any("signal" in i for i in all_issues):
            print("  • Recalc signals:      python scripts/daily_pipeline.py --step calc_signals")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Data health check")
    parser.add_argument("--days", type=int, default=20,
                        help="How many trading days to inspect (default: 20)")
    args = parser.parse_args()

    print(f"{BOLD}Quantamental Data Health Check — {date.today()}{RESET}")
    print(f"Checking last {args.days} trading days\n")

    writer     = get_writer()
    all_issues = []

    all_issues += check_ohlcv(writer, args.days)
    all_issues += check_macro(writer)
    all_issues += check_signals(writer)
    all_issues += check_portfolio()

    print_summary(all_issues)
    sys.exit(1 if all_issues else 0)


if __name__ == "__main__":
    main()
