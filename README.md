# Researchability

[![CI](https://github.com/cdo03c/researchability/actions/workflows/ci.yml/badge.svg)](https://github.com/cdo03c/researchability/actions/workflows/ci.yml)
[![Docs](https://github.com/cdo03c/researchability/actions/workflows/docs.yml/badge.svg)](https://github.com/cdo03c/researchability/actions/workflows/docs.yml)

Automated, staged binary-gate reproducibility and research integrity scoring engine, inspired by the [Institute for Replication (I4R)](https://www.i4replication.org/).

## Features

- **Staged Binary Gates (`configs/gates.yaml`):** Stages 0–5 covering reporting transparency, incentive biases, materials/environment variability, study design & power, statistical rigor, data integrity forensics, sandboxed execution, and numerical output fidelity.
- **Non-Punitive Blocking Semantics:** Separates `FAIL` from `BLOCKED_HARDWARE`, `BLOCKED_ACCESS`, `BLOCKED_DATA_SIZE`, and `N/A`.
- **Zero-Touch vs. Repaired Execution Tracking:** Distinguishes out-of-the-box execution from minor diff-logged repairs.
- **Reproducible Gemini API Integration:** `google-genai` wrapper with strict Pydantic structured outputs, model tiering, and deterministic SQLite response caching.
- **Cross-Field Extensibility:** Starts with `ml_cs` and `bioinformatics` (arXiv & Harvard Dataverse), with declarative profile overlays for `biomedicine_wetlab`, `psychology_social`, and `economics`.

## Installation

```bash
uv sync --extra dev
cp .env.example .env  # Add GEMINI_API_KEY
```

## Quick Start

```bash
# Validate and inspect active gates for a field profile
uv run researchability gates --profile ml_cs

# Run test suite and quality checks
uv run pytest -v --cov=researchability
uv run ruff check .
uv run mypy src
```

## Documentation & Changelog

- Documentation lives in [`docs/`](docs/index.md) and builds with `uv sync --extra docs && uv run mkdocs build --strict`.
- Release history is tracked in [`CHANGELOG.md`](CHANGELOG.md).
- License: [`LICENSE`](LICENSE) (MIT).
