"""Command-line interface for the Researchability scoring engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from researchability.connectors import ArtifactResolver, ArxivConnector, DataverseConnector
from researchability.gates import (
    evaluate_stage0_to_3,
    evaluate_stage0_to_5_with_context,
    render_human_dossier,
)
from researchability.llm import GeminiClient
from researchability.models import AuditReport, FieldProfile, GateResult, PaperBundle
from researchability.registry import GateRegistry
from researchability.sandbox import SandboxConfig
from researchability.scoring import compute_audit_report, render_markdown_summary

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
    with ArxivConnector() as connector, ArtifactResolver() as resolver:
        bundle = connector.fetch_paper(arxiv_id, workdir=workdir)
        if resolve_artifacts:
            bundle = resolver.resolve_bundle(bundle, workdir=workdir)

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
    with DataverseConnector() as connector:
        bundle = connector.fetch_dataset(
            persistent_id,
            workdir=workdir,
            download_files=download_files and workdir is not None,
        )

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
    paper = PaperBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    registry = GateRegistry.from_yaml(config)
    llm_client = GeminiClient() if use_llm else None
    dossier_md: str | None = None

    if execute:
        report_obj, ctx = evaluate_stage0_to_5_with_context(
            paper=paper,
            registry=registry,
            workdir=workdir,
            llm=llm_client,
            sandbox_config=SandboxConfig(timeout_seconds=timeout),
        )
        raw_dossier = ctx.extracted_claims.get("human_dossier_markdown")
        if isinstance(raw_dossier, str):
            dossier_md = raw_dossier
    else:
        report_obj = evaluate_stage0_to_3(
            paper=paper,
            registry=registry,
            workdir=workdir,
            llm=llm_client,
        )

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


@app.command()
def batch(
    source: Annotated[
        str,
        typer.Option("--source", help="Repository source (arxiv | dataverse)."),
    ] = "arxiv",
) -> None:
    """Run batch repository scoring (scheduled for Milestone 5)."""
    typer.echo(f"Batch runner for source '{source}' will be implemented in Milestone 5 (TRE-102).")
    raise typer.Exit(code=0)


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
