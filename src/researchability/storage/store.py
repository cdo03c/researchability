"""DuckDB longitudinal warehouse with partitioned Parquet export and trend queries."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from pydantic import BaseModel, Field

from researchability.models import AuditReport, FieldProfile, PaperBundle
from researchability.registry import GateRegistry

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 2
_VALID_PARTITIONS = {"source_repo", "profile", "published_year"}


class FunnelDropoffRow(BaseModel):
    """Number and share of audited papers clearing each pipeline stage (0-5)."""

    stage: int
    papers_cleared: int
    total_papers: int
    clearance_rate: float


class FailureModeRow(BaseModel):
    """Frequency of FAIL or BLOCKED_* outcomes per gate and domain."""

    gate_id: str
    problem_domain: str
    status: str
    count: int
    share_of_audited: float


class TemporalTrendRow(BaseModel):
    """Aggregated reproducibility metrics grouped by repository, field, and year."""

    source_repo: str
    profile: str
    published_year: str
    paper_count: int
    mean_composite_score: float | None = None
    mean_funnel_depth: float
    zero_touch_rate: float | None = None
    repaired_rate: float | None = None


class AnalyticsSummary(BaseModel):
    """Structured meta-science summary across all stored audit reports."""

    total_papers: int
    funnel_dropoff: list[FunnelDropoffRow] = Field(default_factory=list)
    failure_modes: list[FailureModeRow] = Field(default_factory=list)
    temporal_trends: list[TemporalTrendRow] = Field(default_factory=list)


class ResearchabilityStore:
    """Longitudinal DuckDB store supporting resumable batches and Parquet export."""

    def __init__(self, db_path: Path = Path("results/researchability.duckdb")) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        conn = duckdb.connect(str(self.db_path))
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL,
                    applied_at TIMESTAMP NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    paper_id VARCHAR PRIMARY KEY,
                    source_repo VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    authors_json VARCHAR NOT NULL,
                    published_year INTEGER,
                    doi VARCHAR,
                    primary_profile VARCHAR NOT NULL,
                    artifact_link_count INTEGER NOT NULL,
                    code_file_count INTEGER NOT NULL,
                    data_file_count INTEGER NOT NULL,
                    ingested_at TIMESTAMP NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_reports (
                    paper_id VARCHAR PRIMARY KEY,
                    source_repo VARCHAR NOT NULL,
                    profile VARCHAR NOT NULL,
                    published_year INTEGER,
                    generated_at TIMESTAMP NOT NULL,
                    funnel_depth INTEGER NOT NULL,
                    composite_score DOUBLE,
                    unweighted_pass_rate DOUBLE,
                    zero_touch_reproducible BOOLEAN,
                    repaired_reproducible BOOLEAN,
                    passed_count INTEGER NOT NULL,
                    failed_count INTEGER NOT NULL,
                    blocked_count INTEGER NOT NULL,
                    na_count INTEGER NOT NULL,
                    flags_json VARCHAR NOT NULL,
                    report_json VARCHAR NOT NULL,
                    core_composite_score DOUBLE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gate_results (
                    paper_id VARCHAR NOT NULL,
                    gate_id VARCHAR NOT NULL,
                    stage INTEGER NOT NULL,
                    problem_domain VARCHAR NOT NULL,
                    evaluator_kind VARCHAR NOT NULL,
                    source_repo VARCHAR NOT NULL,
                    profile VARCHAR NOT NULL,
                    published_year INTEGER,
                    status VARCHAR NOT NULL,
                    confidence DOUBLE NOT NULL,
                    rationale VARCHAR NOT NULL,
                    flags_json VARCHAR NOT NULL,
                    evidence_json VARCHAR NOT NULL,
                    model_id VARCHAR,
                    prompt_version VARCHAR,
                    PRIMARY KEY (paper_id, gate_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_runs (
                    paper_id VARCHAR PRIMARY KEY,
                    source_repo VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    error_message VARCHAR,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            conn.execute(
                "ALTER TABLE audit_reports ADD COLUMN IF NOT EXISTS core_composite_score DOUBLE"
            )
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current_version = int(row[0]) if row and row[0] is not None else 0
            if current_version < SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO schema_version VALUES (?, ?)",
                    [SCHEMA_VERSION, now_utc],
                )
                logger.info(
                    "Applied DuckDB warehouse schema v%s at %s", SCHEMA_VERSION, self.db_path
                )

    def save_audit(
        self,
        paper: PaperBundle,
        report: AuditReport,
        registry: GateRegistry | None = None,
    ) -> None:
        """Upsert paper metadata, audit report, per-gate verdicts, and batch status."""
        active_registry = registry or GateRegistry.from_yaml()
        gate_defs = {g.id: g for g in active_registry.all_gates()}
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        gen_ts = report.generated_at.replace(tzinfo=None)

        with self._connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        paper.paper_id,
                        paper.source_repo,
                        paper.title,
                        json.dumps(paper.authors),
                        paper.published_year,
                        paper.doi,
                        paper.primary_profile.value,
                        len(paper.artifacts.links),
                        len(paper.artifacts.code_files),
                        len(paper.artifacts.data_files),
                        now_utc,
                    ],
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO audit_reports (
                        paper_id, source_repo, profile, published_year, generated_at,
                        funnel_depth, composite_score, unweighted_pass_rate,
                        zero_touch_reproducible, repaired_reproducible,
                        passed_count, failed_count, blocked_count, na_count,
                        flags_json, report_json, core_composite_score
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        report.paper_id,
                        report.source_repo,
                        report.profile.value,
                        paper.published_year,
                        gen_ts,
                        report.funnel_depth,
                        report.composite_score,
                        report.unweighted_pass_rate,
                        report.zero_touch_reproducible,
                        report.repaired_reproducible,
                        report.counts.get("pass", 0),
                        report.counts.get("fail", 0),
                        report.counts.get("blocked", 0),
                        report.counts.get("na", 0),
                        json.dumps(report.human_review_flags),
                        report.model_dump_json(),
                        report.core_composite_score,
                    ],
                )
                conn.execute(
                    "DELETE FROM gate_results WHERE paper_id = ?",
                    [report.paper_id],
                )
                for res in report.gate_results:
                    gdef = gate_defs.get(res.gate_id)
                    stage = gdef.stage if gdef else 0
                    domain = gdef.problem_domain.value if gdef else "triage"
                    eval_kind = gdef.evaluator_kind.value if gdef else "deterministic"
                    conn.execute(
                        """
                        INSERT INTO gate_results VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        [
                            report.paper_id,
                            res.gate_id,
                            stage,
                            domain,
                            eval_kind,
                            report.source_repo,
                            report.profile.value,
                            paper.published_year,
                            res.status.value,
                            res.confidence,
                            res.rationale,
                            json.dumps(res.flags),
                            json.dumps([ev.model_dump() for ev in res.evidence]),
                            res.model_id,
                            res.prompt_version,
                        ],
                    )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO batch_runs VALUES (?, ?, ?, ?, ?)
                    """,
                    [paper.paper_id, paper.source_repo, "COMPLETED", None, now_utc],
                )
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise

    def record_failure(self, paper_id: str, source_repo: str, error_message: str) -> None:
        """Record a batch item error so subsequent runs can inspect or retry it."""
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO batch_runs VALUES (?, ?, ?, ?, ?)
                """,
                [paper_id, source_repo, "FAILED", error_message[:2000], now_utc],
            )

    def completed_paper_ids(self) -> set[str]:
        """Return paper IDs already marked COMPLETED for resumable sweeps."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT paper_id FROM batch_runs WHERE status = 'COMPLETED'"
            ).fetchall()
        return {row[0] for row in rows}

    def export_parquet(
        self,
        parquet_root: Path,
        *,
        partition_by: tuple[str, ...] = ("source_repo", "profile", "published_year"),
    ) -> dict[str, Path]:
        """Export normalized DuckDB tables to Hive-partitioned Parquet datasets."""
        for col in partition_by:
            if col not in _VALID_PARTITIONS:
                msg = f"Unsupported partition column '{col}'. Valid: {sorted(_VALID_PARTITIONS)}"
                raise ValueError(msg)

        parquet_root.mkdir(parents=True, exist_ok=True)
        exports: dict[str, Path] = {}

        table_specs: dict[str, tuple[str, tuple[str, ...]]] = {
            "papers": (
                "SELECT *, primary_profile AS profile, "
                "COALESCE(CAST(published_year AS VARCHAR), 'unknown') AS year_part FROM papers",
                tuple("year_part" if c == "published_year" else c for c in partition_by),
            ),
            "audit_reports": (
                "SELECT *, COALESCE(CAST(published_year AS VARCHAR), 'unknown') AS year_part "
                "FROM audit_reports",
                tuple("year_part" if c == "published_year" else c for c in partition_by),
            ),
            "gate_results": (
                "SELECT *, COALESCE(CAST(published_year AS VARCHAR), 'unknown') AS year_part "
                "FROM gate_results",
                tuple("year_part" if c == "published_year" else c for c in partition_by),
            ),
        }

        with self._connect() as conn:
            for table_name, (select_sql, part_cols) in table_specs.items():
                dest_dir = parquet_root / table_name
                staging_dir = parquet_root / f".staging_{table_name}_{id(conn)}"
                backup_dir = parquet_root / f".backup_{table_name}_{id(conn)}"
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                staging_dir.mkdir(parents=True, exist_ok=True)
                try:
                    row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
                    if row_count and row_count[0] > 0:
                        escaped_staging = str(staging_dir).replace("'", "''")
                        part_clause = ", ".join(part_cols)
                        copy_sql = (
                            f"COPY ({select_sql}) TO '{escaped_staging}' "
                            f"(FORMAT PARQUET, PARTITION_BY ({part_clause}), OVERWRITE_OR_IGNORE 1)"
                        )
                        conn.execute(copy_sql)

                    had_existing = dest_dir.exists()
                    if had_existing:
                        if backup_dir.exists():
                            shutil.rmtree(backup_dir)
                        dest_dir.rename(backup_dir)
                    try:
                        staging_dir.rename(dest_dir)
                    except Exception:
                        if had_existing and backup_dir.exists() and not dest_dir.exists():
                            backup_dir.rename(dest_dir)
                        raise
                    finally:
                        shutil.rmtree(backup_dir, ignore_errors=True)
                    exports[table_name] = dest_dir
                finally:
                    shutil.rmtree(staging_dir, ignore_errors=True)

        return exports

    def funnel_dropoff(self, profile: FieldProfile | None = None) -> list[FunnelDropoffRow]:
        """Compute per-stage funnel clearance counts and rates across audited papers."""
        with self._connect() as conn:
            where = "WHERE profile = ?" if profile else ""
            params = [profile.value] if profile else []
            depths = conn.execute(
                f"SELECT funnel_depth FROM audit_reports {where}",
                params,
            ).fetchall()

        total = len(depths)
        rows: list[FunnelDropoffRow] = []
        for stage in range(0, 6):
            cleared = sum(1 for (d,) in depths if int(d) >= stage)
            rate = (cleared / total) if total > 0 else 0.0
            rows.append(
                FunnelDropoffRow(
                    stage=stage,
                    papers_cleared=cleared,
                    total_papers=total,
                    clearance_rate=round(rate, 4),
                )
            )
        return rows

    def failure_modes(
        self,
        *,
        profile: FieldProfile | None = None,
        limit: int = 20,
    ) -> list[FailureModeRow]:
        """Return most frequent gate failure and blocked modes."""
        with self._connect() as conn:
            if profile is not None:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM audit_reports WHERE profile = ?",
                    [profile.value],
                ).fetchone()
                total_papers = int(total_row[0]) if total_row else 0
                rows = conn.execute(
                    """
                    SELECT gate_id, problem_domain, status, COUNT(*) AS cnt
                    FROM gate_results
                    WHERE (status = 'FAIL' OR status LIKE 'BLOCKED_%') AND profile = ?
                    GROUP BY gate_id, problem_domain, status
                    ORDER BY cnt DESC, gate_id ASC
                    LIMIT ?
                    """,
                    [profile.value, limit],
                ).fetchall()
            else:
                total_row = conn.execute("SELECT COUNT(*) FROM audit_reports").fetchone()
                total_papers = int(total_row[0]) if total_row else 0
                rows = conn.execute(
                    """
                    SELECT gate_id, problem_domain, status, COUNT(*) AS cnt
                    FROM gate_results
                    WHERE status = 'FAIL' OR status LIKE 'BLOCKED_%'
                    GROUP BY gate_id, problem_domain, status
                    ORDER BY cnt DESC, gate_id ASC
                    LIMIT ?
                    """,
                    [limit],
                ).fetchall()

        return [
            FailureModeRow(
                gate_id=str(gate_id),
                problem_domain=str(domain),
                status=str(status),
                count=int(cnt),
                share_of_audited=round((int(cnt) / total_papers), 4) if total_papers > 0 else 0.0,
            )
            for gate_id, domain, status, cnt in rows
        ]

    def temporal_trends(self, *, profile: FieldProfile | None = None) -> list[TemporalTrendRow]:
        """Aggregate reproducibility metrics by source repository, field profile, and year."""
        with self._connect() as conn:
            where_clause = "WHERE profile = ?" if profile is not None else ""
            params: list[object] = [profile.value] if profile is not None else []
            rows = conn.execute(
                f"""
                SELECT
                    source_repo,
                    profile,
                    COALESCE(CAST(published_year AS VARCHAR), 'unknown') AS yr,
                    COUNT(*) AS n_papers,
                    AVG(composite_score) AS mean_score,
                    AVG(funnel_depth) AS mean_depth,
                    AVG(
                        CASE
                            WHEN zero_touch_reproducible IS NULL THEN NULL
                            WHEN zero_touch_reproducible THEN 1.0
                            ELSE 0.0
                        END
                    ) AS zero_touch_rate,
                    AVG(
                        CASE
                            WHEN repaired_reproducible IS NULL THEN NULL
                            WHEN repaired_reproducible THEN 1.0
                            ELSE 0.0
                        END
                    ) AS repaired_rate
                FROM audit_reports
                {where_clause}
                GROUP BY source_repo, profile, yr
                ORDER BY yr ASC, source_repo ASC, profile ASC
                """,
                params,
            ).fetchall()

        return [
            TemporalTrendRow(
                source_repo=str(repo),
                profile=str(prof),
                published_year=str(yr),
                paper_count=int(n),
                mean_composite_score=round(float(mean_score), 4)
                if mean_score is not None
                else None,
                mean_funnel_depth=round(float(mean_depth), 4),
                zero_touch_rate=round(float(zt_rate), 4) if zt_rate is not None else None,
                repaired_rate=round(float(rep_rate), 4) if rep_rate is not None else None,
            )
            for repo, prof, yr, n, mean_score, mean_depth, zt_rate, rep_rate in rows
        ]

    def summary_report(self, profile: FieldProfile | None = None) -> AnalyticsSummary:
        """Compute complete meta-science summary tables."""
        with self._connect() as conn:
            if profile:
                row = conn.execute(
                    "SELECT COUNT(*) FROM audit_reports WHERE profile = ?",
                    [profile.value],
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM audit_reports").fetchone()
        total = int(row[0]) if row else 0
        return AnalyticsSummary(
            total_papers=total,
            funnel_dropoff=self.funnel_dropoff(profile=profile),
            failure_modes=self.failure_modes(profile=profile),
            temporal_trends=self.temporal_trends(profile=profile),
        )


def render_analytics_markdown(summary: AnalyticsSummary) -> str:
    """Render an AnalyticsSummary into Markdown tables for meta-science reporting."""
    lines = [
        "# Researchability Longitudinal Analytics Summary",
        "",
        f"- **Total Audited Papers:** {summary.total_papers}",
        "",
        "## Funnel Drop-off Across Stages 0-5",
        "",
        "| Stage | Papers Cleared | Total | Clearance Rate |",
        "| :---: | ---: | ---: | ---: |",
    ]
    for frow in summary.funnel_dropoff:
        lines.append(
            f"| Stage {frow.stage} | {frow.papers_cleared} | {frow.total_papers} "
            f"| {frow.clearance_rate:.1%} |"
        )

    lines.extend(
        [
            "",
            "## Top Failure & Blocked Modes",
            "",
            "| Gate | Domain | Status | Count | Share of Audited |",
            "| :--- | :--- | :---: | ---: | ---: |",
        ]
    )
    if summary.failure_modes:
        for mrow in summary.failure_modes:
            lines.append(
                f"| `{mrow.gate_id}` | `{mrow.problem_domain}` | `{mrow.status}` "
                f"| {mrow.count} | {mrow.share_of_audited:.1%} |"
            )
    else:
        lines.append("| — | — | — | 0 | 0.0% |")

    lines.extend(
        [
            "",
            "## Temporal & Cross-Field Trends",
            "",
            "| Repo | Profile | Year | Papers | Mean Score | Mean Depth | Zero-Touch | Repaired |",
            "| :--- | :--- | :---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if summary.temporal_trends:
        for trow in summary.temporal_trends:
            score_s = (
                f"{trow.mean_composite_score:.1%}"
                if trow.mean_composite_score is not None
                else "N/A"
            )
            zt_s = f"{trow.zero_touch_rate:.1%}" if trow.zero_touch_rate is not None else "N/A"
            rep_s = f"{trow.repaired_rate:.1%}" if trow.repaired_rate is not None else "N/A"
            lines.append(
                f"| `{trow.source_repo}` | `{trow.profile}` | {trow.published_year} | "
                f"{trow.paper_count} | {score_s} | {trow.mean_funnel_depth:.2f} | "
                f"{zt_s} | {rep_s} |"
            )
    else:
        lines.append("| — | — | — | 0 | N/A | 0.00 | N/A | N/A |")

    return "\n".join(lines) + "\n"
