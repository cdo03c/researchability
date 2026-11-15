"""Command-line interface for the Researchability scoring engine."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from researchability.models import AuditReport, FieldProfile, GateResult, PaperBundle
from researchability.registry import GateRegistry
from researchability.scoring import compute_audit_report, render_markdown_summary

app = typer.Typer(
    name="researchability",
    help="Automated staged binary-gate reproducibility & research integrity scoring.",
    no_args_is_help=True,
)


@app.command()
def gates(
    profile: FieldProfile = typer.Option(
        FieldProfile.ML_CS,
        "--profile",
        "-p",
        help="Disciplinary profile to filter active gates.",
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Optional path to a custom gates.yaml file.",
    ),
) -> None:
    """Validate and list binary gates active for a given disciplinary profile."""
    registry = GateRegistry.from_yaml(config)
    active = registry.gates_for_profile(profile)
    typer.echo(f"Profile '{profile.value}' — {len(active)} active gates (registry v{registry.version}):")
    for gate in active:
        deps = f" [deps: {', '.join(gate.depends_on)}]" if gate.depends_on else ""
        typer.echo(
            f"  Stage {gate.stage} | {gate.id:<34} | {gate.problem_domain.value:<22} | "
            f"{gate.evaluator_kind.value}{deps}"
        )


@app.command()
def score(
    bundle_path: Path = typer.Argument(..., exists=True, help="Path to a PaperBundle JSON file."),
    results_path: Path = typer.Argument(..., exists=True, help="Path to a JSON list of GateResult objects."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Optional AuditReport JSON output path."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Optional custom gates.yaml."),
) -> None:
    """Aggregate pre-evaluated GateResults for a PaperBundle into an AuditReport."""
    import json

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
def batch(
    source: str = typer.Option("arxiv", "--source", help="Repository source (arxiv | dataverse)."),
) -> None:
    """Run batch repository scoring (scheduled for Milestone 5)."""
    typer.echo(f"Batch runner for source '{source}' will be implemented in Milestone 5 (TRE-102).")
    raise typer.Exit(code=0)


@app.command()
def report(
    audit_path: Path = typer.Argument(..., exists=True, help="Path to an AuditReport JSON file."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Optional Markdown output path."),
) -> None:
    """Render a human-readable Markdown audit summary from an AuditReport JSON file."""
    audit = AuditReport.model_validate_json(audit_path.read_text(encoding="utf-8"))
    md = render_markdown_summary(audit)
    if output:
        output.write_text(md, encoding="utf-8")
        typer.echo(f"Wrote Markdown report to {output}")
    else:
        typer.echo(md)
