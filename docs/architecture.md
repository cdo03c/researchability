# Architecture

## Module Layout

- `configs/gates.yaml` — Declarative gate definitions, stage assignments, profile overlays, weights, and DAG dependencies (also bundled into built wheels as `researchability/data/gates.yaml`).
- `src/researchability/models.py` — Typed Pydantic schemas (`PaperBundle`, `ArtifactManifest`, `GateDefinition`, `GateResult`, `DomainScore`, `AuditReport`).
- `src/researchability/registry.py` — Loads `gates.yaml`, validates acyclic stage-ordered dependencies, and filters active gates by `FieldProfile`.
- `src/researchability/llm/client.py` — `GeminiClient` wrapper around `google-genai` enforcing `temperature=0.0`, Pydantic structured outputs, `FAST`/`REASONING` model tiers, and a SQLite cache keyed by `(paper_id, gate_id, prompt_version, model_id, input_hash)`.
- `src/researchability/scoring/aggregator.py` — Propagates dependency outcomes, calculates `funnel_depth` (`0`–`5`), domain pass rates, composite score, `zero_touch_reproducible` vs. `repaired_reproducible`, and Markdown summaries.
- `src/researchability/cli.py` — Typer CLI (`researchability`).

## Reproducible LLM Usage

All LLM-assisted evaluations pin `temperature=0.0` and record `model_id` and `prompt_version` on every `GateResult`. Cached responses are served directly from SQLite even offline, ensuring repeatable scoring across runs without redundant API calls.
