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

## Project Roadmap Status

- **Milestone 1 (Complete):** Core schemas, `gates.yaml` DAG registry, Gemini API structured client with SQLite cache, and scoring aggregator.
- **Milestone 2 (Complete):** arXiv connector, Harvard Dataverse connector, and outbound artifact resolver (GitHub, Zenodo, OSF, DOIs).
- **Milestone 3 (Complete):** Automated Stage 0–3 gate evaluators, schema profiler, and GRIM/`p`-value forensic checks.
- **Milestone 4 (Complete):** Hardened Docker sandbox runner, separate zero-touch (`G4.4`) vs. diff-logged repair (`G4.5`) execution, Stage 5 numeric verification (`G5.1`), artifact checks (`G5.2`), and I4R-style human debate dossier synthesis (`G5.3`).
- **Milestone 5 (Complete):** DuckDB/partitioned-Parquet longitudinal store, resumable batch sweep runner, trend analytics queries, and human ground-truth calibration benchmark.
- **Upcoming (M6):** Cross-field overlays (Biomedicine, Psychology, Economics) and companion meta-science scripts.
