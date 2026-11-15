# User Guide

## What Is Researchability?

**Researchability** is an automated audit and scoring tool that measures how repeatable a scientific paper is—and flags structural, statistical, and technical barriers—before human researchers spend hours trying to reproduce it by hand.

Inspired by the [Institute for Replication (I4R)](https://www.i4replication.org/), it separates **mechanical verification** from **scientific judgment**:

1. **Automated Pre-Audit:** Checks whether data and code links exist and resolve, whether data-prep scripts are included, whether software environments and biological/data references are pinned, whether sample sizes and uncertainty metrics are reported, and whether code executes cleanly to match published numbers.
2. **Human Judgment Handoff:** Produces a structured `AuditReport` and Markdown review dossier highlighting every failed gate, blocked resource, and forensic flag—so human reviewers can focus on debating validity, robustness, and interpretation.
3. **Longitudinal Meta-Science:** Scores large samples of papers from repositories like **arXiv** and **Harvard Dataverse** to study how reproducibility practices change across years and disciplines.

---

## How It Works: Staged Binary Gates

Instead of a single subjective grade, Researchability evaluates each paper through **six sequential stages (Stages 0–5)** covering six reproducibility problem domains:

- **Stage 0 — Triage:** Does the paper make empirical or quantitative claims, and which disciplinary profile applies (`ml_cs`, `bioinformatics`, or cross-field overlays)?
- **Stage 1 — Transparency & Incentive Bias:** Are data and code links provided, accessible, and non-stub? Is data-preparation code included? Are methods, preregistration status, and null/failure cases disclosed?
- **Stage 2 — Materials & Environment Variability:** Are `README` instructions, pinned dependency manifests, random seeds, compute requirements, and domain references (e.g., genome builds/accessions) present?
- **Stage 3 — Study Design, Statistical Rigor & Data Integrity:** Are sample sizes, power/budget justifications, controls/baselines, effect sizes with uncertainty intervals, and multiple-testing controls reported? Do provided datasets match claimed $N$ and pass consistency checks?
- **Stage 4 — Sandboxed Execution:** Does the code build and execute in an isolated container? Out-of-the-box execution (`G4.4`) is tracked separately from minor diff-logged repairs (`G4.5`).
- **Stage 5 — Output Fidelity & Human Dossier:** Do generated outputs match reported results, and is the human review dossier ready?

### Understanding Gate Statuses

| Status | What It Means | Example |
| :--- | :--- | :--- |
| `PASS` | Criterion verified with provenance evidence | GitHub repo resolves and contains pipeline scripts |
| `FAIL` | Criterion explicitly failed | Code link returns HTTP 404, or only a `README` stub exists |
| `BLOCKED_ACCESS` | Restricted by IRB, embargo, or credentials | Harvard Dataverse files marked `restricted: true` |
| `BLOCKED_HARDWARE` | Requires GPU/compute beyond sandbox budget | Multi-GPU training run on a CPU-only runner |
| `BLOCKED_DATA_SIZE` | Dataset exceeds configured download/disk cap | Multi-terabyte raw dataset |
| `N/A` | Not applicable or skipped due to upstream gate | Downstream execution skipped because no code link exists |

Separating `BLOCKED_*` from `FAIL` ensures restricted or compute-heavy studies are never misclassified as broken code in trend statistics.

### Complete Gate Reference: Checks & How They Are Tested

All **38 binary gates** defined in `configs/gates.yaml` (31 core gates evaluated for every paper plus 7 disciplinary overlay gates) run in DAG dependency order across Stages 0–5:

- **`deterministic`**: Tested purely with local parsers, HTTP/manifest inspection, AST/regex static checks, statistical math, or Docker exit codes.
- **`hybrid`**: Checked deterministically from manifests, files, or code first, and uses structured extraction / LLM judgment when heuristics alone cannot confirm (or during diff-logged repair).
- **`llm_assisted`**: Tested via structured Gemini prompts (`FAST` or `REASONING` tier, cached in SQLite) when `--use-llm` is passed, with built-in deterministic regex/artifact fallbacks in `--offline` mode.

| Gate ID | Stage | Domain | Profiles | Kind | What It Checks & How It Is Tested |
| :--- | :---: | :--- | :--- | :--- | :--- |
| `G0.1_EMPIRICAL_CLAIM` | 0 | `triage` | `core` | `llm_assisted` | **Empirical claims present.** Offline checks for ≥2 quantitative indicators (`accuracy`, `f1`, `auc`, `p <`, `n =`, `table 1`, etc.) or bundled tables/code/data; `--use-llm` asks Gemini (`FAST`) to classify empirical vs. pure theory/survey. |
| `G1.1_DATA_LINK_PRESENT` | 1 | `transparency_reporting` | `core` | `hybrid` | **Data source stated.** Checks `artifacts.links` for `kind="data"` or local `data_files`; if none extracted, `--use-llm` scans manuscript text (`FAST`) for where evaluation data can be obtained. |
| `G1.2_DATA_ACCESSIBLE` | 1 | `transparency_reporting` | `core` | `deterministic` | **Data link resolves & accessible.** Verifies local downloaded data files exist first; otherwise checks resolved data links for `RESOLVED` (`PASS`), restricted access/IRB (`BLOCKED_ACCESS`), size-cap overflow (`BLOCKED_DATA_SIZE`), or 404 (`FAIL`). |
| `G1.3_CODE_LINK_PRESENT` | 1 | `transparency_reporting` | `core` | `hybrid` | **Code link or files present.** Checks `artifacts.links` for `kind="code"` or `code_files`; if none extracted, `--use-llm` scans manuscript text for repository/archive references. |
| `G1.4_CODE_NON_STUB` | 1 | `transparency_reporting` | `core` | `deterministic` | **Code repo resolves & non-stub.** Verifies cloned/downloaded `code_files` exist or outbound code links resolved without returning `STUB_ONLY`, `BLOCKED_ACCESS`, or `BROKEN_404`. |
| `G1.5_DATA_PREP_CODE` | 1 | `transparency_reporting` | `core` | `hybrid` | **Data-preparation code included.** Matches prep-style filenames (`prep`, `clean`, `etl`, `split`, etc.) or code calls (`read_csv`, `read_parquet`, `train_test_split`, `dropna`, `to_csv`) across `code_files`. |
| `G1.6_METHOD_COMPLETENESS` | 1 | `transparency_reporting` | `core` | `llm_assisted` | **Methods & parameters complete.** Offline checks for ≥2 parameter/protocol terms (`learning rate`, `batch size`, `hyperparameter`, `cross-validation`, `inclusion criteria`, etc.); `--use-llm` asks Gemini (`FAST`) whether parameter/split rules allow independent repetition. |
| `G1.7_PREREGISTRATION_DISCLOSED` | 1 | `incentive_bias` | `core` | `llm_assisted` | **Preregistration or exploratory status.** Checks for `kind="prereg"` links; otherwise `--use-llm` (or offline regex matching `preregister`, `pre-analysis plan`, `aspredicted`, `clinicaltrials.gov`, or `"exploratory study/analysis"`) checks disclosure. |
| `G1.8_NULL_NEGATIVE_REPORTING` | 1 | `incentive_bias` | `core` | `llm_assisted` | **Limitations or null/negative cases.** Offline checks for `limitation`, `negative result`, `failure case`, `no significant difference`, etc.; `--use-llm` evaluates whether failure modes or negative comparisons are reported. |
| `G1.9_ARRIVE_CONSORT_CHECKLIST` | 1 | `transparency_reporting` | `biomedicine_wetlab` | `hybrid` | **Biomedical reporting checklist.** Offline scans manuscript for `ARRIVE`, `CONSORT`, `STROBE`, `PRISMA`, `SPIRIT`, or `MIAME`; `--use-llm` verifies checklist adherence. |
| `G1.10_PSYCH_PAP_ADHERENCE` | 1 | `incentive_bias` | `psychology_social` | `hybrid` | **Preregistered PAP & deviation clarity.** Checks verified `prereg` / `aspredicted` / `clinicaltrials` / registered `osf` links or PAP deviation language; `--use-llm` checks confirmatory vs. exploratory separation. |
| `G2.1_README_ENTRYPOINT` | 2 | `materials_variability` | `core` | `hybrid` | **README execution order & repo hygiene.** Verifies plain-text run commands, diffs README file references against actual repo scripts/data (`audit_readme_inventory`), checks competing versioned entrypoints, flags divergent same-basename files (`detect_duplicate_files`), repeated output overwrites (`detect_output_collisions`), and static missing-input/hardcoded paths (`audit_script_paths`). |
| `G2.2_ENV_MANIFEST` | 2 | `materials_variability` | `core` | `deterministic` | **Dependency manifest & undeclared-import audit.** Checks `artifacts.manifest_files` for `pyproject.toml`, `requirements.txt`, `environment.yml`, `renv.lock`, or `Dockerfile`, and flags Python/R imports missing from declared manifests (`audit_dependencies`). |
| `G2.3_VERSIONS_PINNED` | 2 | `materials_variability` | `core` | `deterministic` | **Dependency versions pinned & registry live.** Checks lockfiles or pinned specifiers (`==`, `~=`, non-`:latest` Docker tag), fails on unpinned `devtools::install_github`/`pak` remote installs, and supports PyPI/CRAN liveness checks (`check_registry=True`). |
| `G2.4_STOCHASTIC_CONTROLS` | 2 | `materials_variability` | `core` | `hybrid` | **Random seeds / determinism.** Scans `code_files` for seed APIs (`random.seed`, `np.random.seed`, `torch.manual_seed`, `tf.random.set_seed`, `set.seed`, `PYTHONHASHSEED`) or manuscript multi-seed/determinism statements. |
| `G2.5_HARDWARE_SPECS` | 2 | `materials_variability` | `core` | `llm_assisted` | **Hardware/compute specs disclosed.** Offline checks for GPU/CPU/RAM/runtime terms (`a100`, `h100`, `v100`, `gpu`, `cpu`, `gb ram`, `hours`, etc.); `--use-llm` extracts compute & runtime disclosures. |
| `G2.6_BIO_REFERENCE_PINNED` | 2 | `materials_variability` | `bioinformatics` | `llm_assisted` | **Reference genomes & accessions pinned.** Offline scans for `GRCh37`/`GRCh38`/`hg38`/`mm10`, `GENCODE`/`Ensembl` releases, `GSE*`/`SRR*`/`PRJNA*`, `RefSeq`, or `UniProt`; `--use-llm` verifies pinning. |
| `G2.7_WETLAB_MATERIALS_RRID` | 2 | `materials_variability` | `biomedicine_wetlab` | `llm_assisted` | **Reagent RRIDs & cell-line auth.** Offline checks for `RRID:*`, catalog numbers, `STR profiling`, or mycoplasma testing; `--use-llm` evaluates biological material authentication. |
| `G2.8_PROTOCOL_REPOSITORY_LINK` | 2 | `materials_variability` | `biomedicine_wetlab` | `hybrid` | **Protocol repository or wet-lab steps.** Checks for `protocols.io`/`bio-protocol` links, or ≥2 quantitative reagent/timing parameters (`mM`, `µM`, `mg/mL`, `°C`, `rpm`); `--use-llm` verifies step-level completeness. |
| `G2.9_STATA_R_PACKAGE_VALID` | 2 | `materials_variability` | `economics` | `deterministic` | **Stata/R package structure.** Verifies `.do`/`.ado`/`.R`/`.Rmd`/`.py`/`.sas`/`.sps` scripts exist alongside a `README`, master entrypoint (`master`, `00_`, `main`, `run_all`), or ICPSR/Dataverse/Zenodo/OSF archive link. |
| `G3.1_SAMPLE_SIZE_STATED` | 3 | `study_design` | `core` | `llm_assisted` | **Sample sizes & split counts stated.** First runs deterministic `extract_claimed_sample_sizes` regexes (`N=`, cohort/sample/dataset counts); falls back to `--use-llm` (`FAST`) if no counts are extracted. |
| `G3.2_POWER_OR_BUDGET_JUSTIFIED` | 3 | `study_design` | `core` | `llm_assisted` | **Power analysis or run budget justified.** Offline matches power analysis, minimum detectable effect, `1 - beta`, N-seed, or k-fold cross-validation justifications; `--use-llm` checks with `REASONING` tier. |
| `G3.3_CONTROLS_AND_BASELINES` | 3 | `study_design` | `core` | `llm_assisted` | **Controls & comparative baselines.** Offline searches for `baseline`, `ablation`, `control group`, `placebo`, `state-of-the-art`, or comparative language; `--use-llm` evaluates experimental baselines. |
| `G3.4_EFFECT_SIZES_UNCERTAINTY` | 3 | `statistical_rigor` | `core` | `llm_assisted` | **Effect sizes & uncertainty intervals.** Offline checks for `±`, `95% CI`, `confidence interval`, `standard error`, `SD`, or `IQR` (flagging bare p-values/point estimates); `--use-llm` checks with `REASONING` tier. |
| `G3.5_MULTIPLE_TESTING_CONTROLLED` | 3 | `statistical_rigor` | `core` | `llm_assisted` | **Multiple-testing control.** Offline passes if correction terms (`Bonferroni`, `Benjamini-Hochberg`, `FDR`, `Holm`, `q-value`) appear or ≤1 p-value is present; `--use-llm` checks with `REASONING` tier. |
| `G3.6_LEAKAGE_HYGIENE` | 3 | `statistical_rigor` | `core` | `hybrid` | **Train/test leakage hygiene.** Statically inspects `code_files` to flag `.fit()` or `.fit_transform()` invoked prior to `train_test_split(...)`. |
| `G3.7_DATA_READABLE_MATCHES_N` | 3 | `data_integrity` | `core` | `hybrid` | **Data parse, claimed N, sentinels & PII.** Profiles `.csv`/`.csv.gz`, `.tsv`/`.tsv.gz`, `.json`, `.jsonl`, `.npy`, and `.parquet` files (`profile_data_file`), checks claimed `N` (±10%, min ±2), detects unredacted PII headers/emails/IPs (`PII_LEAK_DETECTED`), `100% NA` columns, and sentinel missing-value codes (`-1/-2/-3`, `-999`, `9999`) paired with unguarded threshold slices. |
| `G3.8_EXCLUSIONS_DOCUMENTED` | 3 | `data_integrity` | `core` | `hybrid` | **Exclusions, sentinel filters & NA->0 coercion.** Scans `code_files` for filtering calls (`.dropna(`, `.drop_duplicates(`, `.query(`, `na.omit(`, `subset(`), unguarded sentinel threshold slices (`scan_code_sentinel_risks`), and blind `fillna(0)`/`replace_na(0)`/`recode .=0` coercions against manuscript disclosures. |
| `G3.9_NUMERIC_CONSISTENCY` | 3 | `data_integrity` | `core` | `deterministic` | **Forensic numeric & table consistency.** Runs `statcheck` (`t`, `z`, `chi2` vs reported `p`), paired-mean `GRIM` granularity checks, regression `coef*** (se)` significance-star vs normal `|coef/se|` checks, confidence-interval bound/containment checks (`check_table_stars_and_cis`), and `d`/`r`/`R^2`/SD range checks (`check_effect_size_bounds`). |
| `G3.10_ECON_ROBUSTNESS_SPECIFICATION` | 3 | `statistical_rigor` | `economics`, `psychology_social` | `hybrid` | **Robustness & placebo specifications.** Detects robustness methods (`placebo test`, `falsification`, `cluster standard errors`, `fixed effects`, `event study`, `diff-in-diff`, `IV`, `specification curve`) and records a `repframe`-ready spec in context. |
| `G4.1_SANDBOX_FEASIBLE` | 4 | `execution_output` | `core` | `deterministic` | **Sandbox compute & data preflight.** Runs `check_sandbox_feasibility` against local/remote dataset sizes (`max_data_bytes`) and hard GPU markers (`.cuda()`, `deepspeed`, `flash_attn`, etc. without CPU fallback), returning `BLOCKED_DATA_SIZE` or `BLOCKED_HARDWARE` if exceeded. |
| `G4.2_ENV_BUILDS` | 4 | `execution_output` | `core` | `deterministic` | **Container image builds.** Builds an isolated non-root (`65534:65534`) Docker image from the repo's `Dockerfile` or a generated `python:3.11-slim` / `rocker/r-ver:4.4.1` Dockerfile from `requirements.txt`/`pyproject.toml`/`renv.lock`. |
| `G4.3_PREP_EXECUTES` | 4 | `execution_output` | `core` | `deterministic` | **Data-prep pipeline runs.** Executes discovered data-preparation scripts inside the offline (`--network=none`, `--cap-drop=ALL`, resource-capped, `/data:ro`) Docker container and verifies exit code `0`. |
| `G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH` | 4 | `execution_output` | `core` | `deterministic` | **Main analysis runs zero-touch.** Discovers the main entrypoint (from `README` commands or `main`/`run`/`analysis`/etc.) and runs the unmodified repository inside the offline Docker sandbox, checking exit code `0`. |
| `G4.5_ANALYSIS_EXECUTES_REPAIRED` | 4 | `execution_output` | `core` | `hybrid` | **Main analysis runs after logged repair.** Marked `N/A` if `G4.4` passed; otherwise applies bounded mechanical patches (hardcoded local paths, missing directory creation, missing local file copies, optional `--use-llm` `REASONING` minimal diff ≤20 lines), logs unified diffs, and re-runs in Docker. |
| `G5.1_NUMERIC_OUTCOMES_MATCH` | 5 | `execution_output` | `core` | `hybrid` | **Reproduced metrics & execution log audit.** Compares claimed metrics against generated JSON/CSV/TSV/log/stdout metrics (`abs_diff <= 0.005` or `rel_diff <= 2%`) and scans post-run logs for convergence warnings, high `R-hat`, `dropped singleton` fixed-effect observations, and model `N_obs` vs. manuscript `N` mismatches (`audit_execution_logs`). |
| `G5.2_FIGURES_REGENERATE` | 5 | `execution_output` | `core` | `deterministic` | **Key tables & figures regenerate.** Inspects files created/modified during sandbox execution (`G4.4`/`G4.5`) for non-empty figure (`.png`, `.pdf`, `.svg`, `.jpg`, `.eps`, `.tiff`) or table (`.csv`, `.tsv`, `.tex`, `.json`, `.xlsx`) artifacts. |
| `G5.3_HUMAN_DOSSIER_READY` | 5 | `execution_output` | `core` | `hybrid` | **Human review dossier synthesized.** Compiles all gate outcomes, forensic/hygiene/package flags, execution-log and effective-`N` diagnostics, numeric comparison table, repair unified diffs, and robustness checks into an I4R-style Markdown dossier. |

---

## Quickstart Walkthrough

### 1. Install and Configure

```bash
uv sync --extra dev
cp .env.example .env
```

Open `.env` and set your Google Gemini API key if you will run LLM-assisted gates:

```env
GEMINI_API_KEY=your_actual_key_here
```

### 2. Inspect Gates for Your Field

See every binary gate active for a disciplinary profile:

```bash
uv run researchability gates --profile ml_cs
uv run researchability gates --profile bioinformatics
```

### 3. Ingest a Paper or Replication Package

Fetch an **arXiv** paper (metadata, LaTeX source or PDF fallback, and resolved outbound links):

```bash
uv run researchability ingest arxiv 2401.00001 \
  --workdir ./workspaces/2401.00001 \
  --output ./workspaces/2401.00001/bundle.json
```

Or fetch a **Harvard Dataverse** dataset (metadata, file manifest, and accessible replication files):

```bash
uv run researchability ingest dataverse "doi:10.7910/DVN/EXAMPLE" \
  --workdir ./workspaces/dvn_example \
  --output ./workspaces/dvn_example/bundle.json
```

*(Pass `--manifest-only` on Dataverse or `--no-resolve-artifacts` on arXiv to inspect metadata without downloading files or cloning repos.)*

### 4. Run Automated Audits, Sandboxed Execution & Generate a Human Dossier

Run the automated Stage 0–3 gate evaluators (offline heuristics by default, or add `--use-llm` to enable Gemini API verification):

```bash
uv run researchability audit ./workspaces/2401.00001/bundle.json \
  --workdir ./workspaces/2401.00001 \
  --output audit_report.json \
  --markdown review_summary.md
```

To execute the paper's preparation and main analysis scripts inside an offline, resource-capped Docker sandbox (Stages 4–5) and generate the full I4R-style human debate dossier:

```bash
uv run researchability audit ./workspaces/2401.00001/bundle.json \
  --workdir ./workspaces/2401.00001 \
  --execute \
  --timeout 300 \
  --output audit_report.json \
  --dossier human_dossier.md
```

Or aggregate pre-evaluated `GateResult` JSONs directly:

```bash
uv run researchability score bundle.json results.json -o audit_report.json
uv run researchability report audit_report.json -o review_dossier.md
```

### 5. Run Longitudinal Batch Sweeps & Calibrate Against Human Audits

Run a resumable batch sweep over arXiv categories or Dataverse queries (persisting into DuckDB and Hive-partitioned Parquet):

```bash
uv run researchability batch --source arxiv -C cs.LG -C q-bio.QM --limit 50 \
  --db results/researchability.duckdb \
  --parquet-dir results/parquet \
  --markdown results/analytics.md
```

Query longitudinal funnel drop-off rates and temporal trends from an existing warehouse, or calibrate gate precision/recall/Cohen's $\kappa$ against human labels:

```bash
uv run researchability analytics --db results/researchability.duckdb -m results/analytics.md
uv run researchability calibrate ground_truth.jsonl -o cal.json -m cal.md
```

### 6. Using Researchability from Python

```python
from pathlib import Path
from researchability.connectors import ArxivConnector, ArtifactResolver
from researchability.models import FieldProfile
from researchability.registry import GateRegistry

registry = GateRegistry.from_yaml()
print("Active bioinformatics gates:", len(registry.gates_for_profile(FieldProfile.BIOINFORMATICS)))

with ArxivConnector() as arxiv, ArtifactResolver() as resolver:
    # Search arXiv by category
    hits = arxiv.search(categories=["cs.LG", "q-bio.QM"], max_results=5)
    if hits:
        bundle = arxiv.fetch_paper(hits[0].arxiv_id, workdir=Path("./workspaces/demo"))
        bundle = resolver.resolve_bundle(bundle, workdir=Path("./workspaces/demo"))
        print(bundle.paper_id, len(bundle.artifacts.links), bundle.artifacts.code_files)
```

---

## Automated Hygiene & Forensic Flags Reference

Beyond binary `PASS`/`FAIL` verdicts, Stage 2, 3, and 5 evaluators emit structured warning prefixes in `GateResult.flags` (and aggregate them into the `G5.3` human debate dossier under **Forensic & Methodology Flags**, **Repository & Package Hygiene Audit**, and **Execution Log & Effective-N Audit**):

| Stage / Gate | Flag / Diagnostic | Trigger Condition | Blocking? |
| :--- | :--- | :--- | :---: |
| `G2.1` | `NON_PLAIN_TEXT_README`, `STALE_README_FILE_REF` | Binary-only README (`.docx`/`.pdf`/`.rtf`) lacking plain-text run steps, or README cites files absent from the repository | `FAIL` |
| `G2.1` | `DUPLICATE_DIVERGENT_FILE`, `OUTPUT_COLLISION` | Identically named files across folders with different SHA-256 hashes, or multiple non-append writes overwriting the same output path | `FAIL` |
| `G2.1` | `COMPETING_ENTRYPOINTS`, `UNDOCUMENTED_*`, `HARDCODED_LOCAL_PATH`, `MISSING_REFERENCED_FILE` | Multiple versioned entrypoints (`FINAL`, `v2`), undocumented code/data files, `setwd()`/`cd` local paths, or unresolved script input paths | Advisory flag |
| `G2.2` / `G2.3` | `UNDECLARED_IMPORTS` | Python/R libraries imported in scripts but omitted from manifest files | Advisory flag |
| `G2.3` | `UNPINNED_NON_CRAN_PACKAGE`, `ARCHIVED_OR_MISSING_PACKAGE` | Unpinned `devtools::install_github` / `pak::pkg_install`, or `404`/archived PyPI/CRAN response when `ctx.extracted_claims["check_registry"] = True` (disabled offline by default) | `FAIL` |
| `G3.7` / `G3.8` | `PII_LEAK_DETECTED`, `SENTINEL_MISSING_CODES`, `EMPTY_DATA_COLUMN` | Sensitive headers (`email`, `ip_address`, `phone`, `ssn`, coordinates), email/IPv4 cell values, `100% NA` columns, or survey sentinel codes (`-1/-2/-3`, `-999`, `9999`) | `FAIL` on PII, all-empty table, or sentinel + unguarded filter |
| `G3.8` | `UNGUARDED_SENTINEL_FILTER`, `BLIND_NA_TO_ZERO` | Inequality comparisons on sentinel-coded variables without negative guards, or `.fillna(0)` / `replace_na(0)` / `recode .=0` | `FAIL` on unguarded sentinel filter or undisclosed `NA->0` |
| `G3.9` | `Significance star mismatch`, `Confidence interval mismatch`, `GRIM inconsistency`, `Implausibly large standardized effect size`, `Out-of-range correlation` / `R^2` | Starred `coef (SE)` normal `p` contradicts `*`/`**`/`***`, point estimate outside `[lo, hi]` or inverted CI, failed paired `GRIM`, `|Cohen's d| > 3`, `|r| > 1`, `R^2 ∉ [0, 1]`, negative SD/SE, or `p ∉ [0, 1]` | `FAIL` |
| `G5.1` / `G5.3` | `CONVERGENCE_WARNING`, `DROPPED_SINGLETONS`, `EFFECTIVE_N_MISMATCH` | Post-run logs report `did not converge` / `singular fit` / `R-hat > 1.05`, `reghdfe`/`feols` singleton drops, or model `N_obs` diverging from claimed `N` | Advisory flag in `G5.1` & section in `G5.3` |

---

## Project Roadmap Status

- **Milestone 1 (Complete):** Core schemas, `gates.yaml` DAG registry, Gemini API structured client with SQLite cache, and scoring aggregator.
- **Milestone 2 (Complete):** arXiv connector, Harvard Dataverse connector, and outbound artifact resolver (GitHub, Zenodo, OSF, DOIs).
- **Milestone 3 (Complete):** Automated Stage 0–3 gate evaluators, schema profiler, and GRIM/`p`-value forensic checks.
- **Milestone 4 (Complete):** Hardened Docker sandbox runner, separate zero-touch (`G4.4`) vs. diff-logged repair (`G4.5`) execution, Stage 5 numeric verification (`G5.1`), artifact checks (`G5.2`), and I4R-style human debate dossier synthesis (`G5.3`).
- **Milestone 5 (Complete):** DuckDB/partitioned-Parquet longitudinal store, resumable batch sweep runner, trend analytics queries, and human ground-truth calibration benchmark.
- **Milestone 6 (Complete):** Cross-field overlays (`biomedicine_wetlab`, `psychology_social`, `economics`), OSF/PsyArXiv & OpenICPSR connectors, cross-field `core_composite_score`, and downstream `repframe` export hooks.
- **Milestone 7 (In Progress — Top 6 Priorities Complete):** Implemented I4R-derived README/repo inventory diffing, duplicate/output collision checks, static script path & dependency/registry liveness checks, sentinel missing-value & public PII scans, expanded regression star/CI/effect-bound/GRIM forensics, and post-run convergence/singleton/effective-`N` log audits (`TRE-105`, `TRE-106`, `TRE-108`, `TRE-109`, `TRE-112`, `TRE-113`, `TRE-118`, `TRE-119` Part A, `TRE-120`); remaining follow-on checks (`TRE-107`, `TRE-110`, `TRE-111`, `TRE-114`–`TRE-117`, `TRE-119` Part B, `TRE-121`, `TRE-122`) are tracked in Linear.
