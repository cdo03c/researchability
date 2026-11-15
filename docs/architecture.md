# Architecture

## Module Layout

- `configs/gates.yaml` — Declarative gate definitions, stage assignments, profile overlays, weights, and DAG dependencies (also bundled into built wheels as `researchability/data/gates.yaml`).
- `src/researchability/models.py` — Typed Pydantic schemas (`PaperBundle`, `ArtifactManifest`, `GateDefinition`, `GateResult`, `DomainScore`, `AuditReport`).
- `src/researchability/registry.py` — Loads `gates.yaml`, validates acyclic stage-ordered dependencies, and filters active gates by `FieldProfile`.
- `src/researchability/llm/client.py` — `GeminiClient` wrapper around `google-genai` enforcing `temperature=0.0`, Pydantic structured outputs, `FAST`/`REASONING` model tiers, and a SQLite cache keyed by `(paper_id, gate_id, prompt_version, model_id, input_hash)`.
- `src/researchability/scoring/aggregator.py` — Propagates dependency outcomes, calculates `funnel_depth` (`0`–`5`), domain pass rates, composite score, `zero_touch_reproducible` vs. `repaired_reproducible`, and Markdown summaries.
- `src/researchability/profiler/` — Deterministic tabular/array data profiler and statistical forensic screeners (GRIM granularity, `p`-value consistency).
- `src/researchability/sandbox/` — Hardened Docker execution runner (`--network=none`, CPU/RAM/timeout caps, non-root, `:ro` data mounts) and `G4.1` hardware/size preflight.
- `src/researchability/gates/` — Staged binary-gate evaluators across Stages 0–5 (`stage0_1`, `stage2`, `stage3_design_stats`, `stage3_integrity`, `stage4_execution`, `stage5_verification`, `stage_crossfield`) and orchestrated `runner.py`.
- `src/researchability/connectors/` — Repository ingestion & link resolvers for arXiv, Harvard Dataverse, OSF/PsyArXiv (`OsfConnector`), OpenICPSR (`IcpsrConnector`), GitHub, Zenodo, and protocols.io.
- `src/researchability/storage/` — DuckDB longitudinal warehouse, partitioned Parquet exporter (`source_repo`, `profile`, `year_part`), and funnel/trend analytical queries.
- `src/researchability/batch/` — Resumable, rate-limited batch sweep orchestrator across arXiv, Harvard Dataverse, and bundle directories with per-paper workspace cleanup.
- `src/researchability/calibration/` — Ground-truth human validation benchmark computing confusion matrices, F1, Cohen's $\kappa$, and evaluator-tier rollups.
- `src/researchability/cli.py` — Typer CLI (`researchability`).

## Reproducible LLM Usage

All LLM-assisted evaluations pin `temperature=0.0` and record `model_id` and `prompt_version` on every `GateResult`. Cached responses are served directly from SQLite even offline, ensuring repeatable scoring across runs without redundant API calls.
