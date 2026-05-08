"""Alpha engine persistence and reporting helpers."""

from __future__ import annotations

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


def load_latest_alpha_performance(output_dir: str | Path | None = None) -> dict[str, pd.DataFrame]:
    """Load latest saved alpha performance headline and bucket summary."""
    performance_dir = _output_dir(output_dir) / "performance"
    if not performance_dir.exists():
        return {"headline": pd.DataFrame(), "bucket_summary": pd.DataFrame()}

    headline_path = performance_dir / "alpha_performance_headline_latest.csv"
    if not headline_path.exists():
        headline_path = _latest_csv("alpha_performance_headline_*.csv", performance_dir)

    bucket_path = performance_dir / "alpha_performance_buckets_latest.csv"
    if not bucket_path.exists():
        bucket_path = _latest_csv("alpha_performance_buckets_*.csv", performance_dir)

    headline = pd.read_csv(headline_path) if headline_path and headline_path.exists() else pd.DataFrame()
    bucket_summary = pd.read_csv(bucket_path) if bucket_path and bucket_path.exists() else pd.DataFrame()
    return {"headline": headline, "bucket_summary": bucket_summary}


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


def save_backtest_report(result, output_dir: str | Path | None = None) -> dict[str, Path]:
    out_dir = _output_dir(output_dir) / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    metrics = out_dir / f"backtest_metrics_{stamp}.csv"
    returns = out_dir / f"backtest_daily_returns_{stamp}.csv"
    log = out_dir / f"backtest_rebalance_log_{stamp}.csv"
    result.metrics.to_csv(metrics, index=False)
    result.daily_returns.to_csv(returns, index=False)
    result.rebalance_log.to_csv(log, index=False)
    return {"metrics": metrics, "daily_returns": returns, "rebalance_log": log}


def save_alpha_performance_report(report, output_dir: str | Path | None = None) -> dict[str, Path]:
    """Save AlphaPerformanceReport CSV artifacts."""
    out_dir = _output_dir(output_dir) / "performance"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    rank_log = out_dir / f"alpha_performance_rank_log_{stamp}.csv"
    buckets = out_dir / f"alpha_performance_buckets_{stamp}.csv"
    headline = out_dir / f"alpha_performance_headline_{stamp}.csv"
    latest_headline = out_dir / "alpha_performance_headline_latest.csv"
    latest_buckets = out_dir / "alpha_performance_buckets_latest.csv"
    report.rank_log.to_csv(rank_log, index=False)
    report.bucket_summary.to_csv(buckets, index=False)
    report.headline.to_csv(headline, index=False)
    report.headline.to_csv(latest_headline, index=False)
    report.bucket_summary.to_csv(latest_buckets, index=False)
    return {
        "rank_log": rank_log,
        "bucket_summary": buckets,
        "headline": headline,
        "latest_headline": latest_headline,
        "latest_buckets": latest_buckets,
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
