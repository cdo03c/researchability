# Scoring Methodology

## Gate Outcome Semantics

Every binary gate resolves to `PASS` or `FAIL` when scorable, with explicit non-punitive codes when external barriers or upstream failures apply:

| Status | Meaning | Included in Pass Rate? |
| :--- | :--- | :--- |
| `PASS` | Criterion verified with provenance evidence | Yes (`1`) |
| `FAIL` | Criterion explicitly failed | Yes (`0`) |
| `BLOCKED_HARDWARE` | Requires GPU/compute beyond sandbox budget | No |
| `BLOCKED_ACCESS` | Data restricted (IRB, embargo, paywall) | No |
| `BLOCKED_DATA_SIZE` | Dataset exceeds configured storage limit | No |
| `N/A` | Not applicable to field profile or skipped due to upstream failure | No |

## Dependency Propagation

Gates form a directed acyclic graph across Stages 0–5:

- **`dependency_mode: all`** (default): If any upstream prerequisite is `BLOCKED_*`, downstream gates inherit that `BLOCKED_*` status. If any prerequisite is `FAIL` or `N/A`, downstream gates become `N/A` rather than multiplying failure counts.
- **`dependency_mode: any`**: Used where multiple paths satisfy a prerequisite (for example, Stage 5 output checks accept either `G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH` or `G4.5_ANALYSIS_EXECUTES_REPAIRED`).

## Coverage Across the 6 Reproducibility Problem Domains

| Problem Domain | Stage(s) | Example Gates |
| :--- | :--- | :--- |
| **Poor Reporting & Transparency** | Stage 1 | `G1.1`–`G1.6` (data/code links, non-stub code, data-prep scripts, methods completeness) |
| **Publication & Incentive Biases** | Stage 1 | `G1.7` (preregistration/PAP disclosure), `G1.8` (limitations & null/negative reporting) |
| **Material & Technical Variability** | Stage 2 | `G2.1`–`G2.7` (`README`, pinned manifests, seeds, hardware specs, bio accessions, wet-lab RRIDs) |
| **Study Design & Execution Flaws** | Stage 3 | `G3.1`–`G3.3` (sample sizes, power/budget justification, controls & baselines) |
| **Statistical & Analytical Issues** | Stage 3 | `G3.4`–`G3.6` (effect sizes + uncertainty, multiple-testing control, leakage hygiene) |
| **Data Integrity & Misconduct Screening** | Stage 3 | `G3.7`–`G3.9` (data parses & matches `N`, exclusion audit, GRIM/statcheck/sum checks) |
| **Execution & Output Fidelity** | Stages 4–5 | `G4.1`–`G5.3` (zero-touch vs. repaired execution, metric matching, figure regeneration, human dossier) |
