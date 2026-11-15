# Researchability

**Researchability** is an automated, staged binary-gate reproducibility and research integrity scoring engine inspired by the [Institute for Replication (I4R)](https://www.i4replication.org/).

Its goal is to automate the mechanical, upfront verification of published papers—artifact discovery, reporting transparency, materials and environment checks, study design and statistical screening, data integrity forensics, sandboxed execution, and numerical output matching—so human researchers can reserve their time for scientific judgment, robustness testing, and debate.

## Initial Scope & Extensibility

- **Initial Domains:** Machine Learning, Computer Science, and Bioinformatics (`ml_cs`, `bioinformatics`)
- **Target Repositories:** arXiv and Harvard Dataverse
- **Cross-Field Expansion:** Declarative field-profile overlays (`configs/gates.yaml`) supporting wet-lab biomedicine, psychology, and economics/social sciences

## Installation

```bash
uv sync --extra dev
cp .env.example .env  # Add your GEMINI_API_KEY
```

Verify the gate registry and CLI:

```bash
uv run researchability gates --profile ml_cs
uv run pytest
```
