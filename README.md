# Researchability

Automated, staged binary-gate reproducibility and research integrity scoring engine, inspired by the [Institute for Replication (I4R)](https://www.i4replication.org/).

## Goals
* Automate upfront mechanical verification—artifact discovery, reporting transparency, materials/environment checks, study design & statistical screening, data integrity forensics, sandboxed execution, and numerical output matching—so human reviewers can focus on scientific judgment and robustness.
* Score large paper repositories (starting with **arXiv** and **Harvard Dataverse** across **ML, CS, and Bioinformatics**) to analyze empirical trends in scientific repeatability over time.
* Support modular field overlays (`configs/gates.yaml`) for expansion to biomedicine, psychology, and economics/social sciences.

## Setup

```bash
uv sync --extra dev
cp .env.example .env  # Add GEMINI_API_KEY
```

## Quick Commands

```bash
# Inspect validated binary gates for a field profile
uv run researchability gates --profile ml_cs

# Run test suite
uv run pytest
```
