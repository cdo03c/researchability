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

# Aggregate pre-evaluated gate results into an AuditReport
researchability score paper_bundle.json gate_results.json -o audit_report.json

# Render a Markdown summary for human review
researchability report audit_report.json -o summary.md
```
