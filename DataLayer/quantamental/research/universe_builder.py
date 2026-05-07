"""
Build a filtered research universe from S&P 500/400/600 (S&P 1500).

Two-stage workflow:
    1. Static filters (no DB needed): Wikipedia + Polygon ticker types
       — drops REITs (sector="Real Estate"), ADRs, SPACs, name patterns
    2. Liquidity filters (after backfill): price ≥ $5, ADDV ≥ $2M, age ≥ 252 days
       — uses QuestDB to query backfilled OHLCV

Output: config/research_tickers.json with the filtered universe + metadata.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

WIKIPEDIA_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

# GICS sectors to exclude (REITs primarily)
EXCLUDE_SECTORS = {"Real Estate"}

# Note on ADRs: we do NOT blanket-exclude them. Major foreign company ADRs
# (TSM, ASML, SAP, NVO) are legitimate trading instruments — in fact TSM and
# ASML are in the user's seed candidate list. The S&P 1500 itself acts as the
# liquidity filter (only US-listed stocks meeting market cap / volume thresholds
# make the index). Polygon's ADRC type detection is kept here for potential
# future use but is NOT applied in the static stage.
EXCLUDE_POLYGON_TYPES = {"ADRP", "ADRR", "ADRW", "ETF", "ETN", "ETV"}

# Targeted exclusion list — specific tickers we want out of the research universe
# regardless of S&P 1500 membership. Currently focused on Chinese ADRs with
# known regulatory/accounting concerns. Edit this list to suit your preferences.
HARDCODED_TICKER_EXCLUSIONS = {
    "BABA",   # Alibaba — Chinese regulatory overhang
    "BIDU",   # Baidu
    "JD",     # JD.com
    "PDD",    # PDD Holdings (Temu)
    "NIO",    # NIO (Chinese EV)
}

# Name pattern regex — common SPAC/trust/preferred indicators
SPAC_NAME_PATTERNS = [
    r"\bACQUISITION\b",
    r"\bSPAC\b",
    r"\bTRUST\b",
    r"\bPFD\b",
    r"\bPREFERRED\b",
    r"\bWARRANT\b",
    r"\bUNIT\b\s*$",  # ends with "Unit" (post-IPO units)
]
SPAC_RE = re.compile("|".join(SPAC_NAME_PATTERNS), re.IGNORECASE)

OUTPUT_PATH = Path(__file__).parent.parent / "config" / "research_tickers.json"


# ── Stage 1: Static filters (Wikipedia + Polygon) ─────────────────────────────

# Wikipedia blocks the default urllib User-Agent that pandas.read_html uses.
# Sending a real browser UA via the requests library + Wikipedia's API guidelines
# (a descriptive UA with project name + contact) is the documented workaround.
WIKIPEDIA_USER_AGENT = (
    "QuantamentalResearch/1.0 (Personal research tool; "
    "https://github.com/example/quantamental; contact via repo)"
)


def _fetch_wikipedia_html(url: str, timeout: int = 30) -> str:
    """Fetch a Wikipedia page with a polite UA so we don't get 403'd."""
    resp = requests.get(url, headers={"User-Agent": WIKIPEDIA_USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_sp1500_from_wikipedia() -> pd.DataFrame:
    """Scrape S&P 500 + 400 + 600 constituents from Wikipedia.

    Returns DataFrame with columns: symbol, name, sector, index_source.
    Uses requests + pandas.read_html (requires lxml).
    """
    frames = []

    for index_name, url in WIKIPEDIA_URLS.items():
        try:
            html = _fetch_wikipedia_html(url)
            tables = pd.read_html(StringIO(html))
        except Exception as exc:
            logger.error("Failed to scrape %s: %s", index_name, exc)
            continue

        # The constituent table is always the first one with > 100 rows
        constituents = next((t for t in tables if len(t) > 100), None)
        if constituents is None:
            logger.warning("No constituent table found for %s", index_name)
            continue

        # Wikipedia column names vary slightly across the three pages.
        # Normalise to {symbol, name, sector}.
        cols_lower = {c.lower(): c for c in constituents.columns}
        symbol_col = cols_lower.get("symbol") or cols_lower.get("ticker symbol") or cols_lower.get("ticker")
        name_col = cols_lower.get("security") or cols_lower.get("company")
        sector_col = (
            cols_lower.get("gics sector")
            or cols_lower.get("gics sub-industry")
            or cols_lower.get("sector")
        )

        if not symbol_col:
            logger.warning("%s: cannot find symbol column", index_name)
            continue

        df = pd.DataFrame({
            "symbol": constituents[symbol_col].astype(str).str.strip().str.upper(),
            "name": constituents[name_col].astype(str).str.strip() if name_col else "",
            "sector": constituents[sector_col].astype(str).str.strip() if sector_col else "",
            "index_source": index_name,
        })
        # Wikipedia uses BRK.B style; Polygon uses BRK.B too — leave as-is
        df = df[df["symbol"].str.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", na=False)]
        frames.append(df)
        logger.info("%s: %d constituents scraped", index_name, len(df))

    if not frames:
        raise RuntimeError("No S&P 1500 constituents could be scraped from Wikipedia")

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset="symbol")
    logger.info("S&P 1500 combined: %d unique tickers", len(combined))
    return combined


def fetch_polygon_ticker_metadata(tickers: list[str]) -> pd.DataFrame:
    """Get ticker types from Polygon for a list of symbols.

    Uses /v3/reference/tickers with batch enumeration (1000/page).
    Returns DataFrame with columns: symbol, polygon_type, polygon_active.
    """
    from polygon import RESTClient
    from quantamental.config.settings import POLYGON_API_KEY
    from quantamental.data.ingest.polygon_client import _limiter

    client = RESTClient(api_key=POLYGON_API_KEY)
    requested = set(tickers)
    records = []

    # Polygon's list_tickers can enumerate all active US stocks.
    # We enumerate the union of "type=CS" and "type=ADRC" stocks and intersect with our list.
    for type_filter in ("CS", "ADRC"):
        try:
            _limiter.wait()
            for t in client.list_tickers(market="stocks", active=True, type=type_filter, limit=1000):
                sym = getattr(t, "ticker", None)
                if sym in requested:
                    records.append({
                        "symbol":         sym,
                        "polygon_type":   getattr(t, "type", type_filter),
                        "polygon_active": getattr(t, "active", True),
                    })
        except Exception as exc:
            logger.warning("Polygon list_tickers (type=%s) failed: %s", type_filter, exc)

    df = pd.DataFrame(records).drop_duplicates(subset="symbol")
    logger.info("Polygon metadata fetched: %d/%d tickers matched", len(df), len(requested))
    return df


def apply_static_filters(
    sp1500_df: pd.DataFrame,
    polygon_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Apply REIT/SPAC/name filters. Polygon data is OPTIONAL.

    Wikipedia is the authoritative source for S&P 1500 membership. Polygon
    metadata is best-effort enrichment — its `list_tickers` paginator is
    unreliable for full-universe enumeration (server 504s mid-stream), so we
    don't gate on it. If a polygon_df is provided, we additionally drop
    confirmed ADRP/ADRR/ETF types, but we never drop on "unknown".

    Each step short-circuits when the df becomes empty to avoid pandas dropping
    column metadata when boolean-indexing an empty frame with an empty Series.
    """
    df = sp1500_df.copy()
    if polygon_df is not None and not polygon_df.empty:
        df = df.merge(polygon_df, on="symbol", how="left")

    initial = len(df)
    stats = {"initial_count": initial, "drops": {}}

    def _apply(df: pd.DataFrame, mask: pd.Series, key: str) -> pd.DataFrame:
        stats["drops"][key] = int(mask.sum()) if len(mask) else 0
        return df[~mask] if len(df) else df

    # 1. Drop REITs (Real Estate GICS sector)
    df = _apply(
        df,
        df["sector"].isin(EXCLUDE_SECTORS) if len(df) else pd.Series([], dtype=bool),
        "sector_real_estate",
    )

    # 2. Drop SPAC/trust/warrant/preferred name patterns
    df = _apply(
        df,
        df["name"].fillna("").apply(lambda s: bool(SPAC_RE.search(s)))
            if len(df) else pd.Series([], dtype=bool),
        "name_pattern_spac_trust",
    )

    # 3. Drop targeted hardcoded exclusions (Chinese ADRs etc.) — see constant for list
    df = _apply(
        df,
        df["symbol"].isin(HARDCODED_TICKER_EXCLUSIONS)
            if len(df) else pd.Series([], dtype=bool),
        "hardcoded_exclusions",
    )

    # 4. (Optional) Drop confirmed ADRP/ETF Polygon types — only when polygon_df was provided.
    # We do NOT drop on "polygon_type IS NULL" — missing metadata means unknown, not bad.
    if "polygon_type" in df.columns:
        df = _apply(
            df,
            df["polygon_type"].isin(EXCLUDE_POLYGON_TYPES)
                if len(df) else pd.Series([], dtype=bool),
            "polygon_confirmed_excluded",
        )
        stats["polygon_metadata_unavailable"] = int(df["polygon_type"].isna().sum())

    stats["final_count"] = len(df)
    logger.info("Static filters: %d → %d (drops: %s)", initial, len(df), stats["drops"])
    return df, stats


# ── Stage 2: Liquidity filters (uses QuestDB) ─────────────────────────────────

def apply_liquidity_filters(
    tickers: list[str],
    price_floor: float = 5.0,
    addv_min: float = 2_000_000.0,
    min_history_days: int = 252,
    lookback_days: int = 30,
) -> tuple[list[str], dict]:
    """Filter tickers using QuestDB-backed OHLCV data.

    Drops:
      - Latest close < price_floor
      - Trailing-N-day ADDV (avg close × volume) < addv_min
      - Total history < min_history_days (recent IPOs)

    BASE_CANDIDATES are whitelisted — they bypass all liquidity filters since
    the user has explicitly chosen them as trading targets. ETFs (SPY/QQQ/SMH)
    in particular often have unusual volume patterns and we never want to drop
    them. Recently-added candidates can also bypass the IPO history filter.

    Returns (kept_tickers, stats_dict).
    """
    from quantamental.data.ingest.questdb_writer import query, symbol_list_clause
    from quantamental.config.universe import BASE_CANDIDATE_TICKERS, load_candidate_list

    if not tickers:
        return [], {"reason": "no_input_tickers"}

    # Whitelist: never drop tickers in the active candidate list or BASE seed
    whitelist = {t.upper() for t in BASE_CANDIDATE_TICKERS} | {t.upper() for t in load_candidate_list()}

    in_list, params = symbol_list_clause(tickers)

    sql = f"""
        SELECT
            symbol,
            count()                      AS row_count,
            last(close)                  AS latest_close,
            avg(close * volume)          AS addv_full
        FROM daily_ohlcv
        WHERE symbol IN ({in_list})
        GROUP BY symbol
    """
    summary = query(sql, params)

    if summary.empty:
        logger.warning("No OHLCV data found for any of the %d input tickers", len(tickers))
        return [], {"reason": "no_data_in_db", "input_count": len(tickers)}

    # Trailing-N-day ADDV — separate query so we don't pull all rows
    recent_sql = f"""
        SELECT
            symbol,
            avg(close * volume) AS addv_recent
        FROM daily_ohlcv
        WHERE symbol IN ({in_list})
          AND ts > dateadd('d', -{max(1, int(lookback_days) * 2)}, now())
        GROUP BY symbol
    """
    try:
        recent = query(recent_sql, params)
        summary = summary.merge(recent, on="symbol", how="left")
    except Exception as exc:
        logger.warning("Recent ADDV query failed (%s) — falling back to full ADDV", exc)
        summary["addv_recent"] = summary["addv_full"]

    stats = {"input_count": len(tickers), "drops": {}}

    initial = len(summary)
    not_in_db = set(tickers) - set(summary["symbol"])
    stats["drops"]["not_in_db"] = len(not_in_db)

    # Mark whitelisted rows so filters can spare them
    summary = summary.copy()
    summary["whitelisted"] = summary["symbol"].isin(whitelist)

    # 1. Recent IPO filter (whitelist bypass)
    too_short = (summary["row_count"] < min_history_days) & (~summary["whitelisted"])
    stats["drops"]["recent_ipo"] = int(too_short.sum())
    summary = summary[~too_short]

    # 2. Price floor (whitelist bypass)
    too_cheap = (summary["latest_close"] < price_floor) & (~summary["whitelisted"])
    stats["drops"]["below_price_floor"] = int(too_cheap.sum())
    summary = summary[~too_cheap]

    # 3. ADDV floor (whitelist bypass)
    too_illiquid = (summary["addv_recent"].fillna(0) < addv_min) & (~summary["whitelisted"])
    stats["drops"]["below_addv_floor"] = int(too_illiquid.sum())
    summary = summary[~too_illiquid]

    kept = summary["symbol"].tolist()

    # Add any whitelisted tickers that weren't even in QuestDB (e.g. ETFs that
    # somehow missed backfill — better to keep their slot in the universe and
    # warn loudly than silently drop)
    missing_whitelist = whitelist - set(kept) - not_in_db
    for sym in (whitelist & not_in_db):
        logger.warning(
            "Whitelisted ticker %s has NO data in QuestDB — needs backfill", sym,
        )

    whitelist_kept = sum(summary["whitelisted"]) if "whitelisted" in summary.columns else 0
    stats["whitelist_kept"] = int(whitelist_kept)
    stats["final_count"] = len(kept)
    stats["initial_count"] = initial

    logger.info(
        "Liquidity filters: %d → %d (drops: %s, whitelist kept: %d)",
        len(tickers), len(kept), stats["drops"], int(whitelist_kept),
    )
    return kept, stats


# ── Orchestration ─────────────────────────────────────────────────────────────

def build_static_universe(use_polygon: bool = False) -> dict:
    """Stage 1: scrape Wikipedia, apply REIT/SPAC filters, union with BASE_CANDIDATES.

    The research universe is (S&P 1500 filtered) ∪ (BASE_CANDIDATES).
    Wikipedia's S&P 1500 lists only contain US common stocks — ETFs (SPY, QQQ,
    SMH, EWY) and foreign ADRs (TSM, BABA, ASML) are excluded. Since these are
    in the user's seed candidate list and essential for benchmarks/signals, we
    explicitly union them in so they always get backfilled.

    Polygon enumeration is OFF by default — its full-universe paginator is
    unreliable (server 504s after ~6 min). The Wikipedia + sector + name-pattern
    filters catch ~99% of pathological cases on their own.
    """
    logger.info("=== Building static research universe ===")
    sp1500 = fetch_sp1500_from_wikipedia()

    polygon = None
    if use_polygon:
        try:
            polygon = fetch_polygon_ticker_metadata(sp1500["symbol"].tolist())
        except Exception as exc:
            logger.warning("Polygon enrichment failed (%s) — proceeding with Wikipedia only", exc)

    filtered, stats = apply_static_filters(sp1500, polygon)
    sp1500_filtered = set(filtered["symbol"].tolist())

    # Union with BASE_CANDIDATES so ETFs (SPY/QQQ/SMH/EWY) and foreign ADRs
    # (TSM/ASML) are always included in the research universe.
    from quantamental.config.universe import BASE_CANDIDATE_TICKERS
    base_set = {t.upper() for t in BASE_CANDIDATE_TICKERS}
    forced_in = sorted(base_set - sp1500_filtered)

    union = sp1500_filtered | base_set

    # Re-apply hardcoded exclusion AFTER union — exclusion always wins.
    # This ensures e.g. BABA is removed even though it's in BASE_CANDIDATES.
    blocked_in_union = union & HARDCODED_TICKER_EXCLUSIONS
    final_tickers = sorted(union - HARDCODED_TICKER_EXCLUSIONS)

    stats["base_candidates_forced_in"] = forced_in
    stats["base_candidates_forced_count"] = len(forced_in)
    stats["final_count_after_union"] = len(final_tickers)
    stats["hardcoded_exclusions_blocked"] = sorted(blocked_in_union)

    if forced_in:
        logger.info(
            "Union with BASE_CANDIDATES added %d tickers not in S&P 1500: %s",
            len(forced_in), ", ".join(forced_in),
        )
    if blocked_in_union:
        logger.info(
            "HARDCODED_TICKER_EXCLUSIONS blocked %d tickers from final universe: %s",
            len(blocked_in_union), ", ".join(sorted(blocked_in_union)),
        )

    payload = {
        "stage":         "static",
        "tickers":       final_tickers,
        "ticker_count":  len(final_tickers),
        "filters":       {
            "exclude_sectors":      list(EXCLUDE_SECTORS),
            "name_patterns":        SPAC_NAME_PATTERNS,
            "polygon_enrichment":   use_polygon,
            "polygon_excluded_types": list(EXCLUDE_POLYGON_TYPES) if use_polygon else None,
            "base_candidates_unioned": True,
        },
        "stats":         stats,
        "generated_at":  datetime.now(UTC).isoformat(),
    }
    _write_universe_file(payload)
    logger.info("Wrote %d tickers → %s", len(final_tickers), OUTPUT_PATH)
    return payload


def refine_universe_with_liquidity(
    price_floor: float = 5.0,
    addv_min: float = 2_000_000.0,
    min_history_days: int = 252,
) -> dict:
    """Stage 2: load existing JSON, filter by QuestDB liquidity, save back."""
    logger.info("=== Refining research universe with liquidity filters ===")
    if not OUTPUT_PATH.exists():
        raise FileNotFoundError(
            f"{OUTPUT_PATH} not found. Run build_universe.py --stage static first."
        )

    with OUTPUT_PATH.open() as f:
        payload = json.load(f)

    tickers = payload["tickers"]
    kept, liquidity_stats = apply_liquidity_filters(
        tickers,
        price_floor=price_floor,
        addv_min=addv_min,
        min_history_days=min_history_days,
    )

    payload["stage"] = "refined"
    payload["tickers"] = sorted(kept)
    payload["ticker_count"] = len(kept)
    payload["liquidity_filters"] = {
        "price_floor":      price_floor,
        "addv_min":         addv_min,
        "min_history_days": min_history_days,
    }
    payload["liquidity_stats"] = liquidity_stats
    payload["refined_at"] = datetime.now(UTC).isoformat()

    _write_universe_file(payload)
    logger.info("Refined: %d → %d tickers", len(tickers), len(kept))
    return payload


def _write_universe_file(payload: dict):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as f:
        json.dump(payload, f, indent=2)
