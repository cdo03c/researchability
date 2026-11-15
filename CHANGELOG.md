# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Milestone 2 repository connectors (`TRE-92`, `TRE-93`, `TRE-94`):
  - `ArxivConnector`: Atom API search, LaTeX source archive parsing (with `pypdf` PDF fallback), section/table extraction, and category-to-profile inference.
  - `DataverseConnector`: Harvard Dataverse Search & Dataset APIs, file manifest classification, download budget checks, and `BLOCKED_ACCESS`/`BLOCKED_DATA_SIZE` detection.
  - `ArtifactResolver`: Outbound URL/DOI link classifier and verifier for GitHub (including stub-only detection), Zenodo, OSF, and HTTP targets.
  - CLI `researchability ingest arxiv` and `researchability ingest dataverse` commands.
- User Guide (`docs/user-guide.md`) explaining what Researchability does and how to use it.

- Milestone 3 Stage 0–3 gate evaluators & data-integrity forensics (`TRE-95`–`TRE-98`):
  - Stage 0–1 evaluators (`stage0_1.py`): empirical claim triage, data/code link & prep-script checks, methods completeness, preregistration disclosure, and null/negative reporting.
  - Stage 2 evaluators (`stage2.py`): README entrypoint checks, manifest & pinned-version checks, stochastic seed controls, hardware specs, and bioinformatics reference-build checks (`G2.6`).
  - Stage 3 design & statistical rigor (`stage3_design_stats.py`): sample-size extraction, power/budget checks, controls/baselines, uncertainty intervals, multiple-testing checks, and static pre-split leakage detection.
  - Data profiler & forensic screening (`profiler/data_profiler.py`, `profiler/forensics.py`, `stage3_integrity.py`): CSV/TSV/JSON/JSONL/NPY/Parquet profiling against claimed `N`, undisclosed exclusion audits, GRIM granularity checks, and pure-Python `t`/`z`/`chi2` `p`-value consistency checks.
  - CLI `researchability audit` command running Stage 0–3 end-to-end (`--offline` or `--use-llm`).

### Planned
- Milestone 4: Isolated Docker sandbox execution and output fidelity verification (`TRE-99`–`TRE-101`).

## [0.0.0] - 2026-11-14

### Added
- Initial `researchability` Python package scaffold (`pyproject.toml`, `src/` layout, `py.typed`, MIT `LICENSE`).
- Declarative staged binary-gate specification (`configs/gates.yaml`) covering Stages 0–5 across all six reproducibility problem domains (`triage`, `transparency_reporting`, `incentive_bias`, `materials_variability`, `study_design`, `statistical_rigor`, `data_integrity`, `execution_output`) and field overlays (`ml_cs`, `bioinformatics`, `biomedicine_wetlab`, `psychology_social`, `economics`).
- Core Pydantic domain models (`PaperBundle`, `ArtifactManifest`, `GateDefinition`, `GateResult`, `DomainScore`, `AuditReport`) and DAG-validated `GateRegistry` with wheel-packaged `gates.yaml` fallback.
- Reproducible Google Gemini API client (`researchability.llm.GeminiClient`) using `google-genai`, strict Pydantic structured outputs, `FAST`/`REASONING` model tiers, and deterministic SQLite response caching.
- Scoring aggregator (`researchability.scoring.compute_audit_report`) with dependency propagation (`PASS`, `FAIL`, `BLOCKED_*`, `N/A`), `any`/`all` prerequisite modes, funnel depth calculation, per-domain pass rates, and Markdown dossier summaries.
- Typer CLI (`researchability`) with `gates`, `score`, `batch`, and `report` subcommands.
- CI/CD workflows (`.github/workflows/ci.yml`, `docs.yml`, `release.yml`), pre-commit hooks (`.pre-commit-config.yaml`), MkDocs documentation (`docs/`), and initial unit test suite.
