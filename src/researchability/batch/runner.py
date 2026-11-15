"""Resumable batch sweep orchestrator for arXiv, Harvard Dataverse, and bundle corpora."""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from researchability.connectors import ArtifactResolver, ArxivConnector, DataverseConnector
from researchability.gates import evaluate_stage0_to_3, evaluate_stage0_to_5
from researchability.llm import GeminiClient
from researchability.models import AuditReport, PaperBundle
from researchability.registry import GateRegistry
from researchability.sandbox import DockerSandboxRunner, SandboxConfig
from researchability.storage import AnalyticsSummary, ResearchabilityStore

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


class BatchConfig(BaseModel):
    """Configuration for resumable batch evaluation, rate limiting, and Parquet export."""

    db_path: Path = Path("results/researchability.duckdb")
    parquet_dir: Path | None = Path("results/parquet")
    workspace_root: Path = Path(".researchability_batch_workspaces")
    min_interval_seconds: float = Field(default=0.0, ge=0.0)
    cleanup_workspaces: bool = True
    execute: bool = False
    resolve_artifacts: bool = True
    resume: bool = True
    use_llm: bool = False
    sandbox_timeout_seconds: int = Field(default=300, ge=1)


class BatchTarget(BaseModel):
    """Single target identifier queued for batch ingestion and scoring."""

    paper_id: str
    source_repo: str = Field(description="arxiv | dataverse")


class BatchRunSummary(BaseModel):
    """Execution summary and warehouse analytics produced by a batch sweep."""

    total_targets: int
    processed: int
    skipped_resumed: int
    succeeded: int
    failed: int
    failed_ids: list[str] = Field(default_factory=list)
    parquet_exports: dict[str, str] = Field(default_factory=dict)
    analytics: AnalyticsSummary


def _sanitize_dirname(raw_id: str) -> str:
    return _SAFE_ID_RE.sub("_", raw_id).strip("_") or "paper"


class BatchSweepRunner:
    """Orchestrates resumable batch ingestion, isolated scoring, cleanup, and Parquet export."""

    def __init__(
        self,
        config: BatchConfig | None = None,
        *,
        store: ResearchabilityStore | None = None,
        registry: GateRegistry | None = None,
        arxiv: ArxivConnector | None = None,
        dataverse: DataverseConnector | None = None,
        resolver: ArtifactResolver | None = None,
        llm: GeminiClient | None = None,
        sandbox: DockerSandboxRunner | None = None,
    ) -> None:
        self.config = config or BatchConfig()
        self.store = store or ResearchabilityStore(self.config.db_path)
        self.registry = registry or GateRegistry.from_yaml()
        self._arxiv = arxiv
        self._dataverse = dataverse
        self._resolver = resolver
        self._llm = llm if llm is not None else (GeminiClient() if self.config.use_llm else None)
        self._sandbox = sandbox or DockerSandboxRunner(
            SandboxConfig(timeout_seconds=self.config.sandbox_timeout_seconds)
        )
        self._last_step_ts = 0.0

    def _pace(self) -> None:
        if self.config.min_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_step_ts
        remaining = self.config.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_step_ts = time.monotonic()

    def _evaluate_one(self, paper: PaperBundle, workdir: Path | None) -> AuditReport:
        if self.config.execute:
            return evaluate_stage0_to_5(
                paper=paper,
                registry=self.registry,
                workdir=workdir,
                llm=self._llm,
                sandbox=self._sandbox,
            )
        return evaluate_stage0_to_3(
            paper=paper,
            registry=self.registry,
            workdir=workdir,
            llm=self._llm,
        )

    def _finalize(
        self,
        *,
        total: int,
        processed: int,
        skipped: int,
        succeeded: int,
        failed_ids: list[str],
    ) -> BatchRunSummary:
        parquet_exports: dict[str, str] = {}
        if self.config.parquet_dir is not None:
            exported = self.store.export_parquet(self.config.parquet_dir)
            parquet_exports = {k: str(v) for k, v in exported.items()}
        analytics = self.store.summary_report()
        return BatchRunSummary(
            total_targets=total,
            processed=processed,
            skipped_resumed=skipped,
            succeeded=succeeded,
            failed=len(failed_ids),
            failed_ids=failed_ids,
            parquet_exports=parquet_exports,
            analytics=analytics,
        )

    def run_bundles(
        self,
        papers: Sequence[PaperBundle],
        *,
        workdirs: dict[str, Path] | None = None,
    ) -> BatchRunSummary:
        """Score a sequence of normalized PaperBundles resumably and persist to DuckDB/Parquet."""
        completed = self.store.completed_paper_ids() if self.config.resume else set()
        processed = 0
        skipped = 0
        succeeded = 0
        failed_ids: list[str] = []

        for paper in papers:
            if paper.paper_id in completed:
                skipped += 1
                continue
            self._pace()
            processed += 1
            wd = workdirs.get(paper.paper_id) if workdirs else None
            try:
                report = self._evaluate_one(paper, wd)
                self.store.save_audit(paper, report, registry=self.registry)
                succeeded += 1
            except Exception as exc:
                self.store.record_failure(paper.paper_id, paper.source_repo, str(exc))
                failed_ids.append(paper.paper_id)

        return self._finalize(
            total=len(papers),
            processed=processed,
            skipped=skipped,
            succeeded=succeeded,
            failed_ids=failed_ids,
        )

    def sweep_targets(self, targets: Sequence[BatchTarget]) -> BatchRunSummary:
        """Ingest and score remote/local targets with per-paper isolation and cleanup."""
        completed = self.store.completed_paper_ids() if self.config.resume else set()
        owns_arxiv = self._arxiv is None
        owns_dv = self._dataverse is None
        owns_resolver = self._resolver is None
        arxiv_conn = self._arxiv or ArxivConnector()
        dv_conn = self._dataverse or DataverseConnector()
        resolver_conn = self._resolver or ArtifactResolver()

        processed = 0
        skipped = 0
        succeeded = 0
        failed_ids: list[str] = []

        try:
            self.config.workspace_root.mkdir(parents=True, exist_ok=True)
            for target in targets:
                raw_id = target.paper_id
                bare_id = raw_id.split(":", maxsplit=1)[1] if ":" in raw_id else raw_id
                canonical_candidates = {
                    raw_id,
                    bare_id,
                    f"arxiv:{bare_id}",
                    f"doi:{bare_id}",
                    f"dataverse:{bare_id}",
                }
                if canonical_candidates & completed:
                    skipped += 1
                    continue

                self._pace()
                processed += 1
                wd = self.config.workspace_root / _sanitize_dirname(target.paper_id)
                wd.mkdir(parents=True, exist_ok=True)
                try:
                    if target.source_repo == "arxiv":
                        paper = arxiv_conn.fetch_paper(target.paper_id, workdir=wd)
                    elif target.source_repo == "dataverse":
                        paper = dv_conn.fetch_dataset(target.paper_id, workdir=wd)
                    else:
                        msg = f"Unsupported batch source_repo: {target.source_repo}"
                        raise ValueError(msg)

                    if self.config.resolve_artifacts:
                        paper = resolver_conn.resolve_bundle(paper, workdir=wd)

                    report = self._evaluate_one(paper, wd)
                    self.store.save_audit(paper, report, registry=self.registry)
                    succeeded += 1
                except Exception as exc:
                    self.store.record_failure(target.paper_id, target.source_repo, str(exc))
                    failed_ids.append(target.paper_id)
                finally:
                    if self.config.cleanup_workspaces and wd.exists():
                        shutil.rmtree(wd, ignore_errors=True)
        finally:
            if owns_arxiv:
                arxiv_conn.close()
            if owns_dv:
                dv_conn.close()
            if owns_resolver:
                resolver_conn.close()

        return self._finalize(
            total=len(targets),
            processed=processed,
            skipped=skipped,
            succeeded=succeeded,
            failed_ids=failed_ids,
        )

    def discover_arxiv_targets(
        self,
        *,
        query: str | None = None,
        categories: list[str] | None = None,
        limit: int = 25,
    ) -> list[BatchTarget]:
        """Discover recent arXiv targets stratified by query or subject categories."""
        owns = self._arxiv is None
        conn = self._arxiv or ArxivConnector()
        try:
            records = conn.search(query=query or "", categories=categories, max_results=limit)
            return [BatchTarget(paper_id=rec.arxiv_id, source_repo="arxiv") for rec in records]
        finally:
            if owns:
                conn.close()

    def discover_dataverse_targets(
        self,
        *,
        query: str = "*",
        subtree: str | None = None,
        limit: int = 25,
    ) -> list[BatchTarget]:
        """Discover Harvard Dataverse targets matching a search query."""
        owns = self._dataverse is None
        conn = self._dataverse or DataverseConnector()
        try:
            hits = conn.search(query=query, subtree=subtree, per_page=limit)
            return [BatchTarget(paper_id=hit.global_id, source_repo="dataverse") for hit in hits]
        finally:
            if owns:
                conn.close()
