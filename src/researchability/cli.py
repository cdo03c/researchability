"""Command-line interface for the Researchability scoring engine."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError

from researchability.batch import BatchConfig, BatchSweepRunner, BatchTarget
from researchability.calibration import (
    evaluate_calibration,
    load_calibration_dataset,
    render_calibration_markdown,
)
from researchability.connectors import (
    ArtifactResolver,
    ArxivConnector,
    DataverseConnector,
    IcpsrConnector,
    OsfConnector,
)
from researchability.gates import (
    evaluate_stage0_to_3,
    evaluate_stage0_to_5_with_context,
    render_human_dossier,
)
from researchability.llm import GeminiApiError, GeminiClient, MissingApiKeyError
from researchability.models import AuditReport, FieldProfile, GateResult, PaperBundle
from researchability.registry import GateRegistry, GateRegistryError
from researchability.sandbox import SandboxConfig
from researchability.scoring import (
    compute_audit_report,
    export_repframe_manifest,
    render_markdown_summary,
)
from researchability.storage import ResearchabilityStore, render_analytics_markdown

app = typer.Typer(
    name="researchability",
    help="Automated staged binary-gate reproducibility & research integrity scoring.",
    no_args_is_help=True,
)

ingest_app = typer.Typer(
    name="ingest",
    help="Fetch papers and replication bundles from research repositories.",
    no_args_is_help=True,
)
app.add_typer(ingest_app, name="ingest")


def _configure_logging(verbose: bool) -> None:
    level_name = "INFO" if verbose else os.getenv("RESEARCHABILITY_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@app.callback()
def main_callback(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable structured INFO logging to stderr."),
    ] = False,
) -> None:
    """Configure runtime logging for researchability CLI invocations."""
    _configure_logging(verbose)


@app.command()
def gates(
    profile: Annotated[
        FieldProfile,
        typer.Option(
            "--profile",
            "-p",
            help="Disciplinary profile to filter active gates.",
        ),
    ] = FieldProfile.ML_CS,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Optional path to a custom gates.yaml file.",
        ),
    ] = None,
) -> None:
    """Validate and list binary gates active for a given disciplinary profile."""
    registry = GateRegistry.from_yaml(config)
    active = registry.gates_for_profile(profile)
    typer.echo(
        f"Profile '{profile.value}' — {len(active)} active gates (registry v{registry.version}):"
    )
    for gate in active:
        deps = f" [deps: {', '.join(gate.depends_on)}]" if gate.depends_on else ""
        typer.echo(
            f"  Stage {gate.stage} | {gate.id:<34} | {gate.problem_domain.value:<22} | "
            f"{gate.evaluator_kind.value}{deps}"
        )


@ingest_app.command("arxiv")
def ingest_arxiv(
    arxiv_id: Annotated[str, typer.Argument(help="arXiv identifier or URL (e.g., 2401.00001).")],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional PaperBundle JSON output path."),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", "-w", help="Workspace directory for downloaded source/repos."),
    ] = None,
    resolve_artifacts: Annotated[
        bool,
        typer.Option("--resolve-artifacts/--no-resolve-artifacts", help="Resolve outbound links."),
    ] = True,
) -> None:
    """Ingest an arXiv paper into a normalized PaperBundle."""
    try:
        with ArxivConnector() as connector, ArtifactResolver() as resolver:
            bundle = connector.fetch_paper(arxiv_id, workdir=workdir)
            if resolve_artifacts:
                bundle = resolver.resolve_bundle(bundle, workdir=workdir)
    except (httpx.HTTPError, ValueError, OSError) as exc:
        typer.echo(f"Error ingesting arXiv '{arxiv_id}': {exc}", err=True)
        raise typer.Exit(code=1) from exc

    payload = bundle.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote PaperBundle to {output}")
    else:
        typer.echo(payload)


@ingest_app.command("dataverse")
def ingest_dataverse(
    persistent_id: Annotated[
        str,
        typer.Argument(help="Harvard Dataverse persistentId (e.g., doi:10.7910/DVN/XXXXXX)."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional PaperBundle JSON output path."),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", "-w", help="Workspace directory for downloaded files."),
    ] = None,
    download_files: Annotated[
        bool,
        typer.Option("--download/--manifest-only", help="Download accessible replication files."),
    ] = True,
) -> None:
    """Ingest a Harvard Dataverse dataset into a normalized PaperBundle."""
    if download_files and workdir is None:
        typer.echo(
            "Warning: --download requested without --workdir; fetching manifest only.",
            err=True,
        )
    try:
        with DataverseConnector() as connector:
            bundle = connector.fetch_dataset(
                persistent_id,
                workdir=workdir,
                download_files=download_files and workdir is not None,
            )
    except (httpx.HTTPError, ValueError, OSError) as exc:
        typer.echo(f"Error ingesting Dataverse '{persistent_id}': {exc}", err=True)
        raise typer.Exit(code=1) from exc

    payload = bundle.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote PaperBundle to {output}")
    else:
        typer.echo(payload)


@ingest_app.command("osf")
def ingest_osf(
    node_or_url: Annotated[
        str,
        typer.Argument(help="OSF node/registration ID, PsyArXiv ID, or URL."),
    ],
    preprint: Annotated[
        bool,
        typer.Option("--preprint/--project", help="Fetch from OSF preprints/PsyArXiv endpoint."),
    ] = False,
    profile: Annotated[
        FieldProfile,
        typer.Option("--profile", "-p", help="Target disciplinary profile."),
    ] = FieldProfile.PSYCHOLOGY_SOCIAL,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", "-w", help="Workspace directory for downloaded OSF files."),
    ] = None,
    download_files: Annotated[
        bool,
        typer.Option("--download/--manifest-only", help="Download public OSF storage files."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional output JSON path."),
    ] = None,
) -> None:
    """Ingest an OSF project, preregistration, or PsyArXiv preprint into a PaperBundle."""
    if download_files and workdir is None:
        typer.echo(
            "Warning: --download requested without --workdir; fetching manifest only.",
            err=True,
        )
    try:
        with OsfConnector() as connector:
            if download_files and workdir is not None:
                bundle = connector.fetch_project(
                    node_or_url,
                    is_preprint=preprint,
                    profile=profile,
                    workdir=workdir,
                    download_files=True,
                )
            else:
                bundle = connector.fetch_project(
                    node_or_url,
                    is_preprint=preprint,
                    profile=profile,
                )
    except (httpx.HTTPError, ValueError, OSError) as exc:
        typer.echo(f"Error ingesting OSF '{node_or_url}': {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = bundle.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote PaperBundle to {output}")
    else:
        typer.echo(payload)


@ingest_app.command("icpsr")
def ingest_icpsr(
    project_or_doi: Annotated[
        str,
        typer.Argument(help="OpenICPSR project ID or 10.3886 DOI."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional output JSON path."),
    ] = None,
) -> None:
    """Ingest an OpenICPSR economics/social-science study into a PaperBundle."""
    try:
        with IcpsrConnector() as connector:
            bundle = connector.fetch_project(project_or_doi)
    except (httpx.HTTPError, ValueError, OSError) as exc:
        typer.echo(f"Error ingesting ICPSR '{project_or_doi}': {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = bundle.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote PaperBundle to {output}")
    else:
        typer.echo(payload)


@app.command()
def score(
    bundle_path: Annotated[
        Path,
        typer.Argument(exists=True, help="Path to a PaperBundle JSON file."),
    ],
    results_path: Annotated[
        Path,
        typer.Argument(exists=True, help="Path to a JSON list of GateResult objects."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional AuditReport JSON output path."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Optional custom gates.yaml."),
    ] = None,
) -> None:
    """Aggregate pre-evaluated GateResults for a PaperBundle into an AuditReport."""
    paper = PaperBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    raw_list = json.loads(results_path.read_text(encoding="utf-8"))
    raw_map = {item["gate_id"]: GateResult.model_validate(item) for item in raw_list}
    registry = GateRegistry.from_yaml(config)
    report_obj = compute_audit_report(paper=paper, registry=registry, raw_results=raw_map)
    payload = report_obj.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote AuditReport to {output}")
    else:
        typer.echo(payload)


@app.command()
def audit(
    bundle_path: Annotated[
        Path,
        typer.Argument(exists=True, help="Path to a PaperBundle JSON file."),
    ],
    workdir: Annotated[
        Path | None,
        typer.Option(
            "--workdir", "-w", help="Workspace directory containing downloaded artifacts."
        ),
    ] = None,
    use_llm: Annotated[
        bool,
        typer.Option(
            "--use-llm/--offline",
            help="Use Gemini API for LLM-assisted gates (default: offline heuristics).",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional AuditReport JSON output path."),
    ] = None,
    markdown: Annotated[
        Path | None,
        typer.Option("--markdown", "-m", help="Optional Markdown summary output path."),
    ] = None,
    dossier: Annotated[
        Path | None,
        typer.Option(
            "--dossier",
            "-d",
            help="Optional I4R-style human review & debate dossier Markdown path.",
        ),
    ] = None,
    repframe: Annotated[
        Path | None,
        typer.Option(
            "--repframe",
            help="Optional downstream repframe robustness specification JSON path.",
        ),
    ] = None,
    profile: Annotated[
        FieldProfile | None,
        typer.Option(
            "--profile",
            "-p",
            help="Override bundle primary_profile with a specific disciplinary overlay.",
        ),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute/--no-execute",
            help="Run Stage 4-5 sandboxed Docker execution and output verification.",
        ),
    ] = False,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="Hard timeout in seconds per sandboxed container command (with --execute).",
        ),
    ] = 300,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Optional custom gates.yaml."),
    ] = None,
) -> None:
    """Run automated Stage 0-3 (or Stage 0-5 with --execute) reproducibility gates."""
    try:
        paper = PaperBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
        registry = GateRegistry.from_yaml(config)
        llm_client = GeminiClient() if use_llm else None
        dossier_md: str | None = None
        robustness_spec: dict[str, object] | None = None

        if execute:
            report_obj, ctx = evaluate_stage0_to_5_with_context(
                paper=paper,
                registry=registry,
                workdir=workdir,
                llm=llm_client,
                profile=profile,
                sandbox_config=SandboxConfig(timeout_seconds=timeout),
            )
            raw_dossier = ctx.extracted_claims.get("human_dossier_markdown")
            if isinstance(raw_dossier, str):
                dossier_md = raw_dossier
            raw_spec = ctx.extracted_claims.get("robustness_spec")
            if isinstance(raw_spec, dict):
                robustness_spec = raw_spec
        else:
            report_obj = evaluate_stage0_to_3(
                paper=paper,
                registry=registry,
                workdir=workdir,
                llm=llm_client,
                profile=profile,
            )
    except (
        ValidationError,
        GateRegistryError,
        MissingApiKeyError,
        GeminiApiError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"Audit failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    payload = report_obj.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote AuditReport to {output}")
    else:
        typer.echo(payload)
    if markdown:
        markdown.write_text(render_markdown_summary(report_obj), encoding="utf-8")
        typer.echo(f"Wrote Markdown summary to {markdown}")
    if dossier:
        dossier.write_text(
            dossier_md or render_human_dossier(report_obj, paper=paper),
            encoding="utf-8",
        )
        typer.echo(f"Wrote Human Dossier to {dossier}")
    if repframe:
        manifest = export_repframe_manifest(
            paper,
            report_obj,
            workdir=workdir,
            robustness_spec=robustness_spec,
        )
        repframe.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        typer.echo(f"Wrote repframe manifest to {repframe}")


@app.command()
def batch(
    source: Annotated[
        str,
        typer.Option("--source", help="Source mode: arxiv | dataverse | bundles."),
    ] = "arxiv",
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search query string for arxiv/dataverse."),
    ] = None,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", "-C", help="arXiv category filter (repeatable)."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum papers/datasets to queue."),
    ] = 25,
    bundles_dir: Annotated[
        Path | None,
        typer.Option(
            "--bundles-dir", help="Directory of *.json PaperBundles (with --source bundles)."
        ),
    ] = None,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="DuckDB database path."),
    ] = Path("results/researchability.duckdb"),
    parquet_dir: Annotated[
        Path | None,
        typer.Option("--parquet-dir", help="Directory for partitioned Parquet export."),
    ] = Path("results/parquet"),
    interval: Annotated[
        float,
        typer.Option("--interval", help="Minimum seconds between consecutive items."),
    ] = 0.0,
    resume: Annotated[
        bool,
        typer.Option("--resume/--no-resume", help="Skip paper_ids already COMPLETED in DuckDB."),
    ] = True,
    execute: Annotated[
        bool,
        typer.Option("--execute/--no-execute", help="Run Stage 4-5 Docker sandbox execution."),
    ] = False,
    use_llm: Annotated[
        bool,
        typer.Option("--use-llm/--offline", help="Enable Gemini API verification."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional BatchRunSummary JSON path."),
    ] = None,
    markdown: Annotated[
        Path | None,
        typer.Option("--markdown", "-m", help="Optional analytics Markdown report path."),
    ] = None,
) -> None:
    """Run a resumable batch reproducibility sweep into DuckDB and partitioned Parquet."""
    cfg = BatchConfig(
        db_path=db_path,
        parquet_dir=parquet_dir,
        min_interval_seconds=interval,
        resume=resume,
        execute=execute,
        use_llm=use_llm,
    )
    with BatchSweepRunner(config=cfg) as runner:
        if source == "bundles":
            if bundles_dir is None or not bundles_dir.is_dir():
                typer.echo(
                    "--bundles-dir must be an existing directory when --source bundles",
                    err=True,
                )
                raise typer.Exit(code=2)
            papers = [
                PaperBundle.model_validate_json(p.read_text(encoding="utf-8"))
                for p in sorted(bundles_dir.glob("*.json"))
            ][:limit]
            summary = runner.run_bundles(papers)
        elif source == "arxiv":
            targets: list[BatchTarget] = runner.discover_arxiv_targets(
                query=query,
                categories=category,
                limit=limit,
            )
            summary = runner.sweep_targets(targets)
        elif source == "dataverse":
            targets = runner.discover_dataverse_targets(query=query or "*", limit=limit)
            summary = runner.sweep_targets(targets)
        else:
            typer.echo(
                f"Unsupported --source '{source}'. Use arxiv, dataverse, or bundles.",
                err=True,
            )
            raise typer.Exit(code=2)

    payload = summary.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote BatchRunSummary to {output}")
    else:
        typer.echo(payload)
    if markdown:
        markdown.write_text(render_analytics_markdown(summary.analytics), encoding="utf-8")
        typer.echo(f"Wrote Analytics Markdown to {markdown}")


@app.command()
def analytics(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="DuckDB database path to query."),
    ] = Path("results/researchability.duckdb"),
    profile: Annotated[
        FieldProfile | None,
        typer.Option("--profile", "-p", help="Optional disciplinary profile filter."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional AnalyticsSummary JSON path."),
    ] = None,
    markdown: Annotated[
        Path | None,
        typer.Option("--markdown", "-m", help="Optional Markdown summary path."),
    ] = None,
) -> None:
    """Query DuckDB longitudinal metrics (funnel drop-off, failures, and trends)."""
    store = ResearchabilityStore(db_path)
    summary = store.summary_report(profile=profile)
    payload = summary.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote AnalyticsSummary to {output}")
    else:
        typer.echo(payload)
    if markdown:
        markdown.write_text(render_analytics_markdown(summary), encoding="utf-8")
        typer.echo(f"Wrote Analytics Markdown to {markdown}")


@app.command()
def calibrate(
    dataset_path: Annotated[
        Path,
        typer.Argument(exists=True, help="JSON or JSONL file of CalibrationSample items."),
    ],
    use_llm: Annotated[
        bool,
        typer.Option("--use-llm/--offline", help="Use Gemini API for LLM-assisted gates."),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option("--execute/--no-execute", help="Include Stage 4-5 sandbox execution."),
    ] = False,
    profile: Annotated[
        FieldProfile | None,
        typer.Option("--profile", "-p", help="Override sample primary_profile overlay."),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option("--timeout", help="Hard timeout in seconds per sandboxed container command."),
    ] = 300,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional CalibrationReport JSON path."),
    ] = None,
    markdown: Annotated[
        Path | None,
        typer.Option("--markdown", "-m", help="Optional calibration Markdown report path."),
    ] = None,
) -> None:
    """Validate automated binary gates against human ground-truth audit labels."""
    try:
        samples = load_calibration_dataset(dataset_path)
        llm_client = GeminiClient() if use_llm else None
        report_obj = evaluate_calibration(
            samples,
            llm=llm_client,
            execute=execute,
            profile=profile,
            sandbox_config=SandboxConfig(timeout_seconds=timeout),
        )
    except (
        ValidationError,
        MissingApiKeyError,
        GeminiApiError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"Calibration failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = report_obj.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Wrote CalibrationReport to {output}")
    else:
        typer.echo(payload)
    if markdown:
        markdown.write_text(render_calibration_markdown(report_obj), encoding="utf-8")
        typer.echo(f"Wrote Calibration Markdown to {markdown}")


@app.command()
def report(
    audit_path: Annotated[
        Path,
        typer.Argument(exists=True, help="Path to an AuditReport JSON file."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional Markdown output path."),
    ] = None,
) -> None:
    """Render a human-readable Markdown audit summary from an AuditReport JSON file."""
    audit = AuditReport.model_validate_json(audit_path.read_text(encoding="utf-8"))
    md = render_markdown_summary(audit)
    if output:
        output.write_text(md, encoding="utf-8")
        typer.echo(f"Wrote Markdown report to {output}")
    else:
        typer.echo(md)
