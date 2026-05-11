"""Alpha engine persistence and reporting helpers."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_values

from quantamental.config.settings import PARQUET_DIR


ALPHA_RANK_COLUMNS = [
    "symbol",
    "asof_date",
    "alpha_score",
    "rank",
    "bucket",
    "target_weight",
    "target_cash",
    "deployment_cap",
    "new_buys_allowed",
    "score_components",
]


def _output_dir(output_dir: str | Path | None = None) -> Path:
    path = Path(output_dir or PARQUET_DIR) / "alpha"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_alpha_ranks(ranks: pd.DataFrame, output_dir: str | Path | None = None) -> dict[str, Path]:
    """Save latest and dated alpha ranks as Parquet plus CSV fallback."""
    out_dir = _output_dir(output_dir)
    asof = str(ranks["asof_date"].iloc[0]) if not ranks.empty and "asof_date" in ranks else "unknown"
    dated = out_dir / f"alpha_ranks_{asof}.parquet"
    latest = out_dir / "alpha_ranks_latest.parquet"
    csv = out_dir / f"alpha_ranks_{asof}.csv"
    ranks.to_parquet(dated, index=False)
    ranks.to_parquet(latest, index=False)
    ranks.to_csv(csv, index=False)
    return {"dated": dated, "latest": latest, "csv": csv}


def load_latest_alpha_ranks(output_dir: str | Path | None = None) -> pd.DataFrame:
    latest = _output_dir(output_dir) / "alpha_ranks_latest.parquet"
    if not latest.exists():
        return pd.DataFrame()
    return pd.read_parquet(latest)


def _latest_csv(pattern: str, directory: Path) -> Path | None:
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _frame_manifest(df: pd.DataFrame | None) -> dict[str, object]:
    if df is None or df.empty:
        return {"rows": 0, "min_ts": None, "max_ts": None}
    out: dict[str, object] = {"rows": int(len(df))}
    if "ts" in df.columns:
        ts = pd.to_datetime(df["ts"], errors="coerce")
        out["min_ts"] = ts.min().isoformat() if ts.notna().any() else None
        out["max_ts"] = ts.max().isoformat() if ts.notna().any() else None
    else:
        out["min_ts"] = None
        out["max_ts"] = None
    if "symbol" in df.columns:
        out["symbols"] = sorted(str(sym) for sym in df["symbol"].dropna().unique())
    return out


def build_validation_manifest(
    *,
    report_type: str,
    parameters: dict,
    symbols: list[str],
    inputs=None,
    data_quality_status: str | None = None,
) -> dict:
    """Build reproducibility metadata for saved validation artifacts."""
    manifest = {
        "report_type": report_type,
        "generated_at": datetime.now(UTC).isoformat(),
        "code_commit": _git_commit(),
        "parameters": parameters,
        "universe": sorted(set(symbols)),
        "data_quality_status": data_quality_status,
        "inputs": {},
    }
    if inputs is not None:
        manifest["inputs"] = {
            "ohlcv": _frame_manifest(getattr(inputs, "ohlcv", None)),
            "stock_signals": _frame_manifest(getattr(inputs, "stock_signals", None)),
            "regime_signals": _frame_manifest(getattr(inputs, "regime_signals", None)),
            "sector_signals": _frame_manifest(getattr(inputs, "sector_signals", None)),
            "earnings_events": _frame_manifest(getattr(inputs, "earnings_events", None)),
        }
    return manifest


def load_latest_alpha_performance(output_dir: str | Path | None = None) -> dict:
    """Load latest saved alpha performance headline, buckets, and manifest."""
    performance_dir = _output_dir(output_dir) / "performance"
    if not performance_dir.exists():
        return {"headline": pd.DataFrame(), "bucket_summary": pd.DataFrame(), "manifest": {}}

    headline_path = performance_dir / "alpha_performance_headline_latest.csv"
    if not headline_path.exists():
        headline_path = _latest_csv("alpha_performance_headline_*.csv", performance_dir)

    bucket_path = performance_dir / "alpha_performance_buckets_latest.csv"
    if not bucket_path.exists():
        bucket_path = _latest_csv("alpha_performance_buckets_*.csv", performance_dir)

    headline = pd.read_csv(headline_path) if headline_path and headline_path.exists() else pd.DataFrame()
    bucket_summary = pd.read_csv(bucket_path) if bucket_path and bucket_path.exists() else pd.DataFrame()
    manifest_path = performance_dir / "alpha_performance_manifest_latest.json"
    if not manifest_path.exists():
        manifest_path = _latest_csv("alpha_performance_manifest_*.json", performance_dir)
    manifest = {}
    if manifest_path and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {"status": "UNKNOWN", "detail": "manifest JSON could not be decoded"}
    return {"headline": headline, "bucket_summary": bucket_summary, "manifest": manifest}


def validation_status_from_headline(
    headline: pd.DataFrame,
    *,
    data_quality_status: str | None = None,
) -> dict[str, object]:
    """Return a simple PASS/WATCH/FAIL validation status for dashboard use."""
    dq = str(data_quality_status or "").upper()
    if dq in {"BLOCKED", "FAIL", "FAILED"}:
        return {
            "status": "FAIL",
            "reason": f"data quality status is {dq}",
            "checks": {"data_quality_status": dq},
        }
    if headline.empty:
        return {
            "status": "WATCH",
            "reason": "no validation headline rows",
            "checks": {"data_quality_status": dq or None},
        }

    frame = headline.copy()
    for col in ["top_minus_avoid", "mean_rank_ic", "rank_dates", "observations"]:
        if col in frame:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

    horizon = pd.to_numeric(frame.get("horizon", pd.Series(index=frame.index)), errors="coerce")
    primary = frame[horizon.eq(20)]
    row = primary.iloc[0] if not primary.empty else frame.iloc[0]
    spread = float(row.get("top_minus_avoid", 0) or 0)
    ic = float(row.get("mean_rank_ic", 0) or 0)
    rank_dates = int(row.get("rank_dates", 0) or 0)
    observations = int(row.get("observations", 0) or 0)
    checks = {
        "data_quality_status": dq or None,
        "top_minus_avoid": spread,
        "mean_rank_ic": ic,
        "rank_dates": rank_dates,
        "observations": observations,
    }

    if rank_dates < 4 or observations < 30:
        return {"status": "WATCH", "reason": "validation sample is still small", "checks": checks}
    if spread > 0 and ic > 0:
        return {"status": "PASS", "reason": "positive spread and positive rank IC", "checks": checks}
    if spread < 0 and ic < 0:
        return {"status": "FAIL", "reason": "negative spread and negative rank IC", "checks": checks}
    return {"status": "WATCH", "reason": "mixed validation evidence", "checks": checks}


def persist_alpha_ranks_to_questdb(ranks: pd.DataFrame) -> int:
    """Persist alpha ranks to QuestDB. Explicit opt-in from CLI only."""
    if ranks.empty:
        return 0
    from quantamental.data.ingest.questdb_writer import get_connection

    create_sql = """
    CREATE TABLE IF NOT EXISTS alpha_ranks (
        symbol SYMBOL CAPACITY 2048 INDEX,
        ts TIMESTAMP,
        alpha_score DOUBLE,
        rank INT,
        bucket STRING,
        target_weight DOUBLE,
        target_cash DOUBLE,
        deployment_cap DOUBLE,
        new_buys_allowed BOOLEAN,
        score_components STRING,
        generated_at TIMESTAMP
    ) TIMESTAMP(ts) PARTITION BY MONTH;
    """
    rows = [
        (
            row["symbol"],
            pd.Timestamp(row["asof_date"]).isoformat(),
            float(row["alpha_score"]),
            int(row["rank"]),
            row["bucket"],
            float(row.get("target_weight", 0.0)),
            float(row.get("target_cash", 1.0)),
            float(row.get("deployment_cap", 0.0)),
            bool(row.get("new_buys_allowed", False)),
            row.get("score_components", "{}"),
            datetime.now(UTC).isoformat(),
        )
        for _, row in ranks.iterrows()
    ]
    insert_sql = """
        INSERT INTO alpha_ranks
            (symbol, ts, alpha_score, rank, bucket, target_weight, target_cash,
             deployment_cap, new_buys_allowed, score_components, generated_at)
        VALUES %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(create_sql)
            execute_values(cur, insert_sql, rows)
        conn.commit()
    return len(rows)


def save_backtest_report(result, output_dir: str | Path | None = None, manifest: dict | None = None) -> dict[str, Path]:
    out_dir = _output_dir(output_dir) / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    metrics = out_dir / f"backtest_metrics_{stamp}.csv"
    returns = out_dir / f"backtest_daily_returns_{stamp}.csv"
    log = out_dir / f"backtest_rebalance_log_{stamp}.csv"
    result.metrics.to_csv(metrics, index=False)
    result.daily_returns.to_csv(returns, index=False)
    result.rebalance_log.to_csv(log, index=False)
    paths = {"metrics": metrics, "daily_returns": returns, "rebalance_log": log}
    if manifest is not None:
        manifest_path = out_dir / f"backtest_manifest_{stamp}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        paths["manifest"] = manifest_path
    return paths


def save_alpha_performance_report(
    report,
    output_dir: str | Path | None = None,
    manifest: dict | None = None,
) -> dict[str, Path]:
    """Save AlphaPerformanceReport CSV artifacts."""
    out_dir = _output_dir(output_dir) / "performance"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    rank_log = out_dir / f"alpha_performance_rank_log_{stamp}.csv"
    buckets = out_dir / f"alpha_performance_buckets_{stamp}.csv"
    headline = out_dir / f"alpha_performance_headline_{stamp}.csv"
    manifest_path = out_dir / f"alpha_performance_manifest_{stamp}.json"
    latest_headline = out_dir / "alpha_performance_headline_latest.csv"
    latest_buckets = out_dir / "alpha_performance_buckets_latest.csv"
    latest_manifest = out_dir / "alpha_performance_manifest_latest.json"
    report.rank_log.to_csv(rank_log, index=False)
    report.bucket_summary.to_csv(buckets, index=False)
    report.headline.to_csv(headline, index=False)
    report.headline.to_csv(latest_headline, index=False)
    report.bucket_summary.to_csv(latest_buckets, index=False)
    if manifest is not None:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        latest_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {
        "rank_log": rank_log,
        "bucket_summary": buckets,
        "headline": headline,
        "latest_headline": latest_headline,
        "latest_buckets": latest_buckets,
        **({"manifest": manifest_path, "latest_manifest": latest_manifest} if manifest is not None else {}),
    }


def save_alpha_diagnostic_report(report, output_dir: str | Path | None = None) -> dict[str, Path]:
    """Save AlphaDiagnosticReport CSV artifacts."""
    out_dir = _output_dir(output_dir) / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    components = out_dir / f"alpha_diagnostics_components_{stamp}.csv"
    attribution = out_dir / f"alpha_diagnostics_bucket_attribution_{stamp}.csv"
    recommendations = out_dir / f"alpha_diagnostics_recommendations_{stamp}.csv"
    latest_components = out_dir / "alpha_diagnostics_components_latest.csv"
    latest_attribution = out_dir / "alpha_diagnostics_bucket_attribution_latest.csv"
    latest_recommendations = out_dir / "alpha_diagnostics_recommendations_latest.csv"

    report.component_summary.to_csv(components, index=False)
    report.bucket_attribution.to_csv(attribution, index=False)
    report.recommendations.to_csv(recommendations, index=False)
    report.component_summary.to_csv(latest_components, index=False)
    report.bucket_attribution.to_csv(latest_attribution, index=False)
    report.recommendations.to_csv(latest_recommendations, index=False)
    return {
        "component_summary": components,
        "bucket_attribution": attribution,
        "recommendations": recommendations,
        "latest_component_summary": latest_components,
        "latest_bucket_attribution": latest_attribution,
        "latest_recommendations": latest_recommendations,
    }
