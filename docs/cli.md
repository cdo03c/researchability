# CLI & Configuration

## Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(required for live calls)* | Google Gemini API key used by `google-genai` |
| `RESEARCHABILITY_FAST_MODEL` | `gemini-2.5-flash` | Model for fast Stage 0–2 extraction/triage |
| `RESEARCHABILITY_REASONING_MODEL` | `gemini-2.5-pro` | Model for deeper Stage 3/5 verification |
| `RESEARCHABILITY_CACHE_DIR` | `.researchability_cache` | Directory storing `llm_cache.sqlite` |

## Commands

```bash
# List and validate gates for a disciplinary profile
researchability gates --profile ml_cs
researchability gates --profile bioinformatics

# Run Stage 0–3 offline/LLM audit without container execution
researchability audit bundle.json --workdir ./workspaces/demo -o audit_report.json -m summary.md

# Run full Stage 0–5 audit with isolated Docker execution and I4R human review dossier
researchability audit bundle.json --workdir ./workspaces/demo --execute -o audit_report.json --dossier dossier.md

# Aggregate pre-evaluated gate results into an AuditReport
researchability score paper_bundle.json gate_results.json -o audit_report.json

# Render a Markdown summary for human review
researchability report audit_report.json -o summary.md
```
