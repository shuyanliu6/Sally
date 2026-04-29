"""
Universe configuration.

Three layers:
  - BASE_CANDIDATES: static seed dict (the original 27, grouped by sector)
  - candidate_list: dynamic — loaded from config/candidate_list.json if it exists,
    grouped by sector (so different sectors can drive different signal logic).
    Use load_candidate_list_by_sector() for the grouped view, or
    load_candidate_list() for a flat list.
  - research_universe: auto-generated S&P 1500 filtered list, loaded from
    config/research_tickers.json. Use load_research_universe(); falls back to
    candidate_list if the file doesn't exist.

Candidate list JSON schema (current — sector-aware):
    {
      "sectors": {
        "upstream_compute": ["NVDA", "AMD", ...],
        "cloud_infrastructure": ["MSFT", ...],
        ...
      },
      "updated_at": "2026-04-27T...Z",
      "notes": "rebalance reason"
    }

Legacy flat schema is auto-migrated on read:
    {"tickers": [...], ...}    →    interpreted as {"sectors": {"uncategorized": [...]}}

Backwards compatibility: ALL_TICKERS still exists as an alias for
BASE_CANDIDATE_TICKERS so existing imports don't break.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent
_CANDIDATE_FILE = _CONFIG_DIR / "candidate_list.json"
_RESEARCH_FILE = _CONFIG_DIR / "research_tickers.json"


# ── Base seed (original 27 tickers, the Month 1 starting point) ───────────────

BASE_CANDIDATES = {
    "upstream_compute": ["TSM", "NVDA", "AVGO", "MU", "AMD"],
    "cloud_infrastructure": ["MSFT", "GOOGL", "AMZN", "ORCL", "BABA"],
    "power_cooling": ["VRT", "CEG", "ETN", "GEV", "VST"],
    "application_architecture": ["META", "NOW", "PLTR"],
    "networking": ["MRVL", "ANET", "COHR"],
    "non_us": ["ASML", "EWY"],
    "benchmarks": ["SPY", "QQQ", "SMH"],
}

BASE_CANDIDATE_TICKERS = [t for tickers in BASE_CANDIDATES.values() for t in tickers]

# Legacy aliases — kept so existing imports keep working
UNIVERSE = BASE_CANDIDATES
ALL_TICKERS = BASE_CANDIDATE_TICKERS
BENCHMARKS = BASE_CANDIDATES["benchmarks"]


# ── Dynamic loaders ───────────────────────────────────────────────────────────

# Legacy bucket name used when migrating the old flat schema. Stays around
# (rather than being auto-classified) because the user may want to explicitly
# slot tickers into the right sector via the editor.
UNCATEGORIZED_SECTOR = "uncategorized"


def _read_candidate_json() -> dict | None:
    """Return the parsed candidate_list.json, or None if missing/unreadable."""
    if not _CANDIDATE_FILE.exists():
        return None
    try:
        return json.loads(_CANDIDATE_FILE.read_text())
    except Exception as exc:
        logger.warning("Failed to load %s: %s", _CANDIDATE_FILE, exc)
        return None


def _normalize_sectors(sectors_raw: dict) -> dict[str, list[str]]:
    """Clean a sectors dict: uppercase tickers, dedupe within a sector, sort."""
    out: dict[str, list[str]] = {}
    for sector, tickers in (sectors_raw or {}).items():
        if not isinstance(tickers, list):
            continue
        cleaned = sorted({str(t).strip().upper() for t in tickers if t and str(t).strip()})
        if cleaned:
            out[sector] = cleaned
    return out


def load_candidate_list_by_sector() -> dict[str, list[str]]:
    """Return the candidate list grouped by sector.

    Priority:
      1. config/candidate_list.json (sector-aware schema, or legacy flat schema
         migrated to {"uncategorized": [...]})
      2. BASE_CANDIDATES (the seed sectors)

    Use this when sector identity matters for signal logic. For a flat list,
    use load_candidate_list() — it just flattens this same data.
    """
    data = _read_candidate_json()
    if data is not None:
        # Current schema — sectors dict
        if isinstance(data.get("sectors"), dict):
            normalized = _normalize_sectors(data["sectors"])
            if normalized:
                logger.debug("Loaded candidate sectors from %s: %s",
                             _CANDIDATE_FILE.name,
                             {s: len(t) for s, t in normalized.items()})
                return normalized
        # Legacy flat schema → migrate to a single uncategorized bucket
        if isinstance(data.get("tickers"), list) and data["tickers"]:
            cleaned = sorted({str(t).strip().upper() for t in data["tickers"] if str(t).strip()})
            logger.warning(
                "Loaded legacy flat candidate_list.json with %d tickers — "
                "treating as '%s'. Re-save (CLI or dashboard) to migrate to "
                "the sector-aware schema.",
                len(cleaned), UNCATEGORIZED_SECTOR,
            )
            return {UNCATEGORIZED_SECTOR: cleaned}
    # Fallback: BASE_CANDIDATES
    return {sector: list(tickers) for sector, tickers in BASE_CANDIDATES.items()}


def load_candidate_list() -> list[str]:
    """Return the candidate list as a flat, deduplicated, sorted ticker list.

    Backwards compatible — most callers (pipeline, fundamentals refresh, etc.)
    only need the flat view. Use load_candidate_list_by_sector() when the
    sector buckets matter (per-sector signal logic).
    """
    grouped = load_candidate_list_by_sector()
    flat = sorted({t for tickers in grouped.values() for t in tickers})
    return flat


def load_research_universe() -> list[str]:
    """Return the research universe tickers.

    Priority:
      1. config/research_tickers.json if present (auto-generated by build_universe.py)
      2. load_candidate_list() as fallback (won't break the pipeline if user
         hasn't run build_universe.py yet)
    """
    if _RESEARCH_FILE.exists():
        try:
            data = json.loads(_RESEARCH_FILE.read_text())
            tickers = data.get("tickers", [])
            if tickers:
                logger.debug("Loaded %d research tickers from %s", len(tickers), _RESEARCH_FILE.name)
                return [t.upper() for t in tickers]
        except Exception as exc:
            logger.warning("Failed to load %s: %s — falling back to candidate list", _RESEARCH_FILE, exc)
    return load_candidate_list()


def candidate_list_source() -> str:
    """Return where the current candidate list comes from (for diagnostics)."""
    return "candidate_list.json" if _CANDIDATE_FILE.exists() else "BASE_CANDIDATES (default)"


def candidate_list_metadata() -> dict:
    """Return full metadata for the candidate list JSON, or {} if not present.

    Shape: {"sectors": {sector: [...]}, "updated_at": ISO str, "notes": str}
    """
    return _read_candidate_json() or {}


def save_candidate_list(
    tickers_or_sectors: list[str] | dict[str, list[str]],
    note: str = "",
) -> Path:
    """Write the candidate list to config/candidate_list.json.

    Used by the CLI (`scripts/manage_candidates.py`) and the dashboard editor
    panel — single write path so all sources stay in sync.

    Args:
        tickers_or_sectors: either
            - a flat list of tickers (legacy signature) — saved into the
              "uncategorized" sector so the user can reorganise later
            - a dict {sector: [tickers]} — saved as-is
        note: optional free-form note describing the change

    Returns: path to the written JSON file
    """
    from datetime import datetime as _dt

    if isinstance(tickers_or_sectors, dict):
        sectors = _normalize_sectors(tickers_or_sectors)
    else:
        cleaned = sorted({str(t).strip().upper() for t in tickers_or_sectors if str(t).strip()})
        sectors = {UNCATEGORIZED_SECTOR: cleaned} if cleaned else {}

    total = sum(len(t) for t in sectors.values())
    payload = {
        "sectors": sectors,
        "updated_at": _dt.utcnow().isoformat(timespec="seconds") + "Z",
        "notes": note or "",
    }
    _CANDIDATE_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    logger.info("Saved %d candidates across %d sectors to %s (note: %r)",
                total, len(sectors), _CANDIDATE_FILE.name, note)
    return _CANDIDATE_FILE


def reset_candidate_list() -> bool:
    """Delete config/candidate_list.json so loaders fall back to BASE_CANDIDATES.

    Returns: True if the file existed and was deleted, False if it never existed.
    """
    if _CANDIDATE_FILE.exists():
        _CANDIDATE_FILE.unlink()
        logger.info("Deleted %s — candidate list now falls back to BASE_CANDIDATES", _CANDIDATE_FILE.name)
        return True
    return False


def research_universe_source() -> str:
    """Return where the current research universe comes from."""
    if _RESEARCH_FILE.exists():
        return "research_tickers.json"
    return f"fallback to candidate list ({candidate_list_source()})"

TICKER_METADATA = {
    "TSM":  {"name": "Taiwan Semiconductor",       "sector": "upstream_compute",         "non_us": True},
    "NVDA": {"name": "NVIDIA",                      "sector": "upstream_compute",         "non_us": False},
    "AVGO": {"name": "Broadcom",                    "sector": "upstream_compute",         "non_us": False},
    "MU":   {"name": "Micron Technology",           "sector": "upstream_compute",         "non_us": False},
    "AMD":  {"name": "Advanced Micro Devices",      "sector": "upstream_compute",         "non_us": False},
    "MSFT": {"name": "Microsoft",                   "sector": "cloud_infrastructure",     "non_us": False},
    "GOOGL":{"name": "Alphabet",                    "sector": "cloud_infrastructure",     "non_us": False},
    "AMZN": {"name": "Amazon",                      "sector": "cloud_infrastructure",     "non_us": False},
    "ORCL": {"name": "Oracle",                      "sector": "cloud_infrastructure",     "non_us": False},
    "BABA": {"name": "Alibaba (ADR)",               "sector": "cloud_infrastructure",     "non_us": True},
    "VRT":  {"name": "Vertiv Holdings",             "sector": "power_cooling",            "non_us": False},
    "CEG":  {"name": "Constellation Energy",        "sector": "power_cooling",            "non_us": False},
    "ETN":  {"name": "Eaton",                       "sector": "power_cooling",            "non_us": False},
    "GEV":  {"name": "GE Vernova",                  "sector": "power_cooling",            "non_us": False},
    "VST":  {"name": "Vistra Corp",                 "sector": "power_cooling",            "non_us": False},
    "META": {"name": "Meta Platforms",              "sector": "application_architecture", "non_us": False},
    "NOW":  {"name": "ServiceNow",                  "sector": "application_architecture", "non_us": False},
    "PLTR": {"name": "Palantir",                    "sector": "application_architecture", "non_us": False},
    "MRVL": {"name": "Marvell Technology",          "sector": "networking",               "non_us": False},
    "ANET": {"name": "Arista Networks",             "sector": "networking",               "non_us": False},
    "COHR": {"name": "Coherent Corp",               "sector": "networking",               "non_us": False},
    "ASML": {"name": "ASML Holding (ADR)",          "sector": "non_us",                   "non_us": True},
    "EWY":  {"name": "iShares MSCI South Korea ETF","sector": "non_us",                   "non_us": True},
    "SPY":  {"name": "S&P 500 ETF",                 "sector": "benchmarks",               "non_us": False},
    "QQQ":  {"name": "Nasdaq 100 ETF",              "sector": "benchmarks",               "non_us": False},
    "SMH":  {"name": "VanEck Semiconductor ETF",    "sector": "benchmarks",               "non_us": False},
}

# FRED series IDs
FRED_SERIES = {
    "yield_10y":      "DGS10",       # 10-Year Treasury Yield
    "vix":            "VIXCLS",      # CBOE VIX
    "fed_balance":    "WALCL",       # Fed Total Assets
    "credit_spread":  "BAMLC0A0CM",  # IG OAS Credit Spread
}
