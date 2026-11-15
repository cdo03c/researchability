# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security & Hardening
- Hardened sandbox, storage, extraction, and network boundaries:
  - Disabled execution of arbitrary repository `Dockerfile`s by default (`SandboxConfig.allow_repo_dockerfile = False`) and added `--pids-limit=256`, `--ipc=none`, `--ulimit=nofile=1024:1024`, shell-escaped `shlex.quote` manifest paths, and streamed capped subprocess capture (`default_subprocess_runner`).
  - Added path-traversal / symlink containment (`safe_resolve_under`) and TarSlip/gzip-bomb/streaming byte-cap protections across LaTeX archive extraction, Dataverse file downloads, and Stage 4 script resolution.
  - Enforced strict domain/SSRF validation (`_hostname_matches`, `is_safe_public_url` with optional DNS resolution and per-hop `3xx` redirect checking via `UnsafeUrlError` / `create_safe_http_client`) across connectors, artifact resolution, and registry/preregistration HTTP checks.
  - Switched DuckDB connection/schema management to versioned migrations (`schema_version = 2`), parameterized `analytics` queries, and atomic staging swaps for Hive-partitioned Parquet exports.

### Fixed & Improved
- Fixed Stage 4–5 pipeline correctness bugs: deterministic zero-touch vs. repaired sandbox sub-workspace ordering, AST-safe Python multi-seed header injection (`__future__` / module docstring ordering), exact denominator handling in `audit_model_specifications`, case-insensitive PII header detection (`PII_LEAK_DETECTED`), explicit `domain_scores` propagation through calibration/reporting, and normalized SQLite cache keys + WAL mode in `GeminiClient`.
- Expanded manifest/environment resilience (`environment.yml`, `uv.lock`, `poetry.lock`, `Pipfile`), added bounded `RetryTransport` HTTP retries (`429`/`502`/`503`/`504` + `Retry-After`), wired optional dossier persistence in `BatchSweepRunner`, added structured CLI logging (`--verbose` / `RESEARCHABILITY_LOG_LEVEL`), and exposed `--profile` and `--timeout` on `researchability calibrate`.

### Added
- **Artifact Materialization, Binary Data Profiling, Notebook/Sequential Execution & Starter Calibration Corpus**:
  - Added shared SSRF- and byte-budget-enforced streaming downloader (`stream_download_file`, `download_osf_storage_files` in `connectors/base.py`) and wired file downloads into `ArtifactResolver` (`zenodo_files/`, `osf_files/`), `OsfConnector.fetch_project(..., workdir=..., download_files=True)`, `researchability ingest osf --workdir --download`, `EvaluationContext.candidate_roots()`, and `BatchSweepRunner` (`osf` / `icpsr` targets).
  - Expanded `profiler/data_profiler.py` with `_TabularAccumulator`, pure-Python `.xlsx` parsing (`zipfile` + `defusedxml`), Stata `.dta` binary parsing (formats `113`–`115` headers and `117`–`119` `<stata_dta>` XML/binary rows with sentinels, zero-mass, and means), and `.rds`/`.RData`/`.sav` signature validation.
  - Upgraded Stage 4 execution (`gates/stage4_execution.py`, `sandbox/docker_runner.py`) to run ordered multi-script README workflows (`_discover_main_commands` + sequential error-aware repair), `.ipynb` notebooks via `jupyter nbconvert --execute`, and Julia `.jl` scripts; improved `EvaluationContext.manuscript_snippet` to retain `full_text` alongside `sections` and expanded `G1.8` negative/null phrasing.
  - Added curated starter I4R + cross-field calibration dataset (`examples/calibration_i4r_starter.jsonl`) and ignored generated `results/` / `*.duckdb` artifacts in `.gitignore`.
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
- Milestone 4 Sandboxed Execution & Output Verification (`TRE-99`, `TRE-100`, `TRE-101`):
  - Isolated Docker sandbox harness (`sandbox/docker_runner.py`): Python/R Dockerfile generation, `--network=none`, `--cpus`, `--memory`, `--user=65534:65534`, `--cap-drop=ALL`, read-only `/data:ro` mounts, hard timeouts, created/modified output file diffing, and `G4.1` preflight (`BLOCKED_HARDWARE` / `BLOCKED_DATA_SIZE`).
  - Stage 4 execution gates (`gates/stage4_execution.py`): container build (`G4.2`), data-prep run (`G4.3`), separate unmodified zero-touch execution (`G4.4`), and bounded diff-logged minor mechanical repair execution (`G4.5`).
  - Stage 5 output fidelity verifier & I4R-style human debate dossier generator (`gates/stage5_verification.py`): reported-vs-reproduced numeric metric matching (`G5.1`), non-empty table/figure verification (`G5.2`), and Markdown dossier synthesis (`G5.3`).
  - CLI `researchability audit --execute --dossier ...` supporting full Stage 0–5 evaluation.
- Milestone 5 Batch Pipeline, Analytics & Human Validation (`TRE-102`, `TRE-103`):
  - Longitudinal DuckDB & Hive-partitioned Parquet warehouse (`storage/store.py`): normalized `papers`, `audit_reports`, `gate_results`, and `batch_runs` tables partitioned by `(source_repo, profile, year_part)`, plus SQL queries for funnel drop-off, failure modes, and temporal trends.
  - Resumable batch repository sweep runner (`batch/runner.py`): rate-limited sweeps across arXiv, Harvard Dataverse, or local `PaperBundle` directories with per-paper workspace isolation and cleanup.
  - Ground-truth human calibration benchmark (`calibration/benchmark.py`): computes per-gate confusion matrices (`TP`/`FP`/`FN`/`TN`), Precision, Recall, F1, Cohen's $\kappa$, funnel depth MAE, and deterministic vs. LLM-assisted reliability rollups.
  - Upgraded CLI `researchability batch`, `researchability analytics`, and `researchability calibrate` commands.
- Milestone 6 Cross-Field Extensibility Overlays (`TRE-104`):
  - Extended `configs/gates.yaml` and `gates/stage_crossfield.py` with disciplinary overlays keeping `FieldProfile.CORE` comparable (`core_composite_score` on `AuditReport`):
    - Biomedicine / Wet-Lab (`biomedicine_wetlab`): `G1.9_ARRIVE_CONSORT_CHECKLIST`, `G2.7_WETLAB_MATERIALS_RRID`, and `G2.8_PROTOCOL_REPOSITORY_LINK` (`protocols.io` / `10.17504/protocols.io`).
    - Psychology / Behavioral (`psychology_social`): `G1.10_PSYCH_PAP_ADHERENCE`, OSF / PsyArXiv connector (`connectors/crossfield.py` `OsfConnector`, `psyarxiv.com`, `10.31234/osf.io`), and `researchability ingest osf`.
    - Economics / Social Science (`economics`): `G2.9_STATA_R_PACKAGE_VALID` (`.do`/`.ado`/`.R` support + `stata` language detection), `G3.10_ECON_ROBUSTNESS_SPECIFICATION`, `IcpsrConnector` (`openicpsr.org` / `10.3886` + `researchability ingest icpsr`), and downstream `repframe` robustness manifest export (`export_repframe_manifest` & `researchability audit --repframe`).
- Milestone 7 I4R-Derived Reproducibility Checks (`TRE-105`–`TRE-122`, complete):
  - Repository, lineage & package hygiene (`profiler/hygiene.py`, `profiler/spec_checks.py`, `gates/stage0_1.py`, `gates/stage2.py`): README-to-repo inventory diff (`audit_readme_inventory`), non-plain-text README rejection, competing entrypoint detection, SHA-256 divergent duplicate-basename detection (`detect_duplicate_files`), non-append output overwrite collision detection (`detect_output_collisions`), raw-vs-processed data lineage audit (`audit_data_lineage` in `G1.5`), static path/Stata/isolation/magic-constant/commented-diagnostic audits (`audit_script_paths`, `audit_script_isolation_and_stata`, `audit_constants_and_diagnostics`), and undeclared/unpinned/opt-in PyPI & CRAN liveness checks (`audit_dependencies`).
  - GIS sidecar, zero-mass, sentinel & public PII screening (`profiler/data_profiler.py`, `gates/stage3_integrity.py`): `.shp` companion sidecar & spatial/binary asset completeness (`audit_gis_and_binary_sidecars`), unredacted PII headers/emails/IPv4s (`PII_LEAK_DETECTED`), `100% NA` columns, `log(1+y)` on `>30%` zero-mass outcomes (`zero_mass_columns`), missing column checks, and sentinel codes (`-1/-2/-3`, `-999`, `9999`) paired with unguarded filters and blind `NA -> 0` coercions in `G3.7`/`G3.8`.
  - Code-vs-paper specification & PAP adherence diffs (`profiler/spec_checks.py`, `profiler/robustness.py`, `gates/stage0_1.py`, `gates/stage3_design_stats.py`, `gates/stage_crossfield.py`): regression weight/clustering/split/date-bound cross-checks (`audit_model_specifications` in `G3.6`/`G3.10`) and OSF/web preregistration `N`/primary-outcome diffing (`fetch_preregistration_summary`, `diff_preregistration_against_paper` in `G1.7`/`G1.10`).
  - Expanded table & effect-size forensics (`profiler/forensics.py`): `check_table_stars_and_cis`, `check_effect_size_bounds` (`|Cohen's d| > 3`, `|r| > 1`, `R^2`/`p`/negative SD), and paired `GRIM` checks wired into `G3.9_NUMERIC_CONSISTENCY`.
  - Execution logs, multi-seed sweeps, subsampled fallback & quantitative perturbations (`gates/stage4_execution.py`, `gates/stage5_verification.py`): `audit_execution_logs` (convergence, `R-hat > 1.05`, dropped singletons, `N_obs` mismatch), optional multi-seed sandbox re-runs (`summarize_multiseed_runs` in `G4.4`/`G4.5`/`G5.1`), KS/quantile distribution fallback when `G4.1` is blocked (`compare_distributions`), orphan `Table X`/`Figure Y` coverage (`audit_table_figure_coverage` in `G5.2`), and Bonferroni/BH-FDR + 5%-trim leverage sensitivity checks (`run_quantitative_perturbations` in `G5.3` and `render_human_dossier`).

## [0.0.0] - 2026-11-14

### Added
- Initial `researchability` Python package scaffold (`pyproject.toml`, `src/` layout, `py.typed`, MIT `LICENSE`).
- Declarative staged binary-gate specification (`configs/gates.yaml`) covering Stages 0–5 across all six reproducibility problem domains (`triage`, `transparency_reporting`, `incentive_bias`, `materials_variability`, `study_design`, `statistical_rigor`, `data_integrity`, `execution_output`) and field overlays (`ml_cs`, `bioinformatics`, `biomedicine_wetlab`, `psychology_social`, `economics`).
- Core Pydantic domain models (`PaperBundle`, `ArtifactManifest`, `GateDefinition`, `GateResult`, `DomainScore`, `AuditReport`) and DAG-validated `GateRegistry` with wheel-packaged `gates.yaml` fallback.
- Reproducible Google Gemini API client (`researchability.llm.GeminiClient`) using `google-genai`, strict Pydantic structured outputs, `FAST`/`REASONING` model tiers, and deterministic SQLite response caching.
- Scoring aggregator (`researchability.scoring.compute_audit_report`) with dependency propagation (`PASS`, `FAIL`, `BLOCKED_*`, `N/A`), `any`/`all` prerequisite modes, funnel depth calculation, per-domain pass rates, and Markdown dossier summaries.
- Typer CLI (`researchability`) with `gates`, `score`, `batch`, and `report` subcommands.
- CI/CD workflows (`.github/workflows/ci.yml`, `docs.yml`, `release.yml`), pre-commit hooks (`.pre-commit-config.yaml`), MkDocs documentation (`docs/`), and initial unit test suite.
