# CLI & Configuration

## Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(required for live calls)* | Google Gemini API key used by `google-genai` |
| `RESEARCHABILITY_FAST_MODEL` | `gemini-3.5-flash` | Model for fast Stage 0–2 extraction/triage |
| `RESEARCHABILITY_REASONING_MODEL` | `gemini-3.5-flash` | Model for deeper Stage 3/5 verification |
| `RESEARCHABILITY_CACHE_DIR` | `.researchability_cache` | Directory storing `llm_cache.sqlite` |

## Commands

```bash
# List and validate gates for a disciplinary profile
researchability gates --profile ml_cs
researchability gates --profile bioinformatics

# Run Stage 0–3 offline/LLM audit without container execution
researchability audit bundle.json --workdir ./workspaces/demo -o audit_report.json -m summary.md

# Audit an economics or biomedical study with a field overlay & repframe robustness manifest export
researchability audit econ_bundle.json --workdir ./workspaces/econ --profile economics --repframe repframe.json

# Ingest an OSF preregistration/PsyArXiv preprint or OpenICPSR economics archive
researchability ingest osf https://psyarxiv.com/abcde --preprint -o psych_bundle.json
researchability ingest icpsr 10.3886/E199999V1 -o econ_bundle.json

# Run full Stage 0–5 audit with isolated Docker execution and I4R human review dossier
researchability audit bundle.json --workdir ./workspaces/demo --execute -o audit_report.json --dossier dossier.md

# Aggregate pre-evaluated gate results into an AuditReport
researchability score paper_bundle.json gate_results.json -o audit_report.json

# Render a Markdown summary for human review
researchability report audit_report.json -o summary.md

# Run a resumable batch sweep into DuckDB + partitioned Parquet and print analytics Markdown
researchability batch --source arxiv -C cs.LG -C q-bio.QM --limit 50 \
  --db results/researchability.duckdb --parquet-dir results/parquet -m results/analytics.md

# Query longitudinal funnel drop-off, failure modes, and temporal trends
researchability analytics --db results/researchability.duckdb -m results/analytics.md

# Validate automated binary gates against human-labeled benchmark data
researchability calibrate benchmark.jsonl -o calibration.json -m calibration.md
```
