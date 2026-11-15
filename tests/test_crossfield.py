"""Tests for M6 cross-field overlays (Biomedicine, Psychology, Economics) & repframe export."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from researchability.cli import app
from researchability.connectors import IcpsrConnector, OsfConnector, extract_artifact_links
from researchability.gates import evaluate_stage0_to_3, evaluate_stage0_to_5_with_context
from researchability.gates.base import EvaluationContext
from researchability.gates.stage_crossfield import eval_g1_10_psych_pap_adherence
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    GateStatus,
    PaperBundle,
)
from researchability.registry import GateRegistry
from researchability.sandbox.docker_runner import detect_primary_language
from researchability.scoring import export_repframe_manifest


def test_crossfield_profile_filtering_keeps_core_identical() -> None:
    registry = GateRegistry.from_yaml()
    core_ids = {g.id for g in registry.gates_for_profile(FieldProfile.ML_CS)}
    wet_ids = {g.id for g in registry.gates_for_profile(FieldProfile.BIOMEDICINE_WETLAB)}
    psych_ids = {g.id for g in registry.gates_for_profile(FieldProfile.PSYCHOLOGY_SOCIAL)}
    econ_ids = {g.id for g in registry.gates_for_profile(FieldProfile.ECONOMICS)}

    assert "G1.9_ARRIVE_CONSORT_CHECKLIST" in wet_ids
    assert "G2.7_WETLAB_MATERIALS_RRID" in wet_ids
    assert "G2.8_PROTOCOL_REPOSITORY_LINK" in wet_ids
    assert "G2.7_WETLAB_MATERIALS_RRID" not in psych_ids

    assert "G1.10_PSYCH_PAP_ADHERENCE" in psych_ids
    assert "G3.10_ECON_ROBUSTNESS_SPECIFICATION" in psych_ids
    assert "G2.9_STATA_R_PACKAGE_VALID" in econ_ids
    assert "G3.10_ECON_ROBUSTNESS_SPECIFICATION" in econ_ids
    assert "G2.9_STATA_R_PACKAGE_VALID" not in core_ids

    # Core subset is present across every profile
    for gate in registry.all_gates():
        if FieldProfile.CORE in gate.profiles:
            assert gate.id in wet_ids
            assert gate.id in psych_ids
            assert gate.id in econ_ids


def test_crossfield_link_classification_and_connectors() -> None:
    links = extract_artifact_links(
        "Protocol at https://www.protocols.io/view/assay-123, "
        "preprint https://psyarxiv.com/abcde, "
        "and archive https://www.openicpsr.org/openicpsr/project/199999/version/V1/view."
    )
    hosts = {link.host: link.kind for link in links}
    assert hosts["protocols_io"] == "protocol"
    assert hosts["psyarxiv"] == "other"
    assert hosts["icpsr"] == "data"

    def osf_handler(request: httpx.Request) -> httpx.Response:
        if "/preprints/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "title": "PsyArXiv Working Paper",
                            "description": "Preregistered at https://osf.io/registrations/ab12c",
                            "date_created": "2024-05-10T00:00:00Z",
                            "registration": True,
                        }
                    }
                },
            )
        return httpx.Response(404)

    osf_client = httpx.Client(transport=httpx.MockTransport(osf_handler))
    with OsfConnector(http_client=osf_client) as osf:
        osf_bundle = osf.fetch_project("https://psyarxiv.com/xy12z", is_preprint=True)
    assert osf_bundle.paper_id == "osf:xy12z"
    assert osf_bundle.primary_profile == FieldProfile.PSYCHOLOGY_SOCIAL
    assert osf_bundle.published_year == 2024

    def icpsr_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "title": "Minimum Wage Panel Replication",
                "summary": "Includes master.do and cleaned panel.dta.",
                "publicationDate": "2023-08-01",
                "files": ["README.md", "code/master.do", "data/panel.dta"],
                "restricted": False,
            },
        )

    icpsr_client = httpx.Client(transport=httpx.MockTransport(icpsr_handler))
    with IcpsrConnector(http_client=icpsr_client) as icpsr:
        econ_bundle = icpsr.fetch_project("10.3886/E199999V1")
    assert econ_bundle.paper_id == "icpsr:199999"
    assert econ_bundle.primary_profile == FieldProfile.ECONOMICS
    assert econ_bundle.artifacts.links[0].status == ArtifactStatus.RESOLVED
    assert "code/master.do" in econ_bundle.artifacts.code_files
    assert detect_primary_language(econ_bundle) == "stata"

    # Live-discovered edge cases: versioned PsyArXiv IDs and DataCite fallback on 403
    with OsfConnector(http_client=osf_client) as osf:
        versioned_bundle = osf.fetch_project("67pcn_v2", is_preprint=True)
    assert versioned_bundle.paper_id == "osf:67pcn_v2"

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        if "api.datacite.org" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "titles": [{"title": "DataCite Fallback Study"}],
                            "descriptions": [{"description": "Restricted-use archive"}],
                            "publicationYear": 2026,
                            "rightsList": [{"rights": "Restricted access"}],
                        }
                    }
                },
            )
        return httpx.Response(403, text="Forbidden")

    fallback_client = httpx.Client(transport=httpx.MockTransport(fallback_handler))
    with IcpsrConnector(http_client=fallback_client) as icpsr:
        dc_bundle = icpsr.fetch_project("10.3886/E120001V1")
    assert dc_bundle.title == "DataCite Fallback Study"
    assert dc_bundle.published_year == 2026
    assert dc_bundle.artifacts.links[0].status == ArtifactStatus.BLOCKED_ACCESS


def test_biomedicine_wetlab_overlay_evaluation() -> None:
    text = (
        "We report an in vivo cohort experiment following ARRIVE 2.0 guidelines (n = 48 mice, "
        "p = 0.004, Table 1). HeLa cells were verified by STR profiling and confirmed mycoplasma "
        "negative; anti-GFP antibody RRID:AB_221569 was used (2 mg/mL at 37 °C for 30 min). "
        "Step-by-step protocol is hosted at https://www.protocols.io/view/wetlab-v1."
    )
    paper = PaperBundle(
        paper_id="arxiv:2401.77701",
        source_repo="arxiv",
        title="Wet-Lab Fluorescent Assay Validation",
        published_year=2024,
        primary_profile=FieldProfile.BIOMEDICINE_WETLAB,
        abstract=text,
        full_text=text,
        sections={"methods": text, "results": "95% CI [0.4, 0.9], p = 0.004."},
        artifacts=ArtifactManifest(links=extract_artifact_links(text)),
    )
    report = evaluate_stage0_to_3(paper)
    by_id = {res.gate_id: res for res in report.gate_results}
    assert by_id["G1.9_ARRIVE_CONSORT_CHECKLIST"].status == GateStatus.PASS
    assert by_id["G2.7_WETLAB_MATERIALS_RRID"].status == GateStatus.PASS
    assert by_id["G2.8_PROTOCOL_REPOSITORY_LINK"].status == GateStatus.PASS
    assert report.core_composite_score is not None


def test_psychology_overlay_and_core_comparability() -> None:
    text = (
        "Behavioral experiment preregistered on OSF (https://osf.io/registrations/ab12c) "
        "distinguishing confirmatory vs. exploratory hypotheses, n = 120, power = 0.90, "
        "95% CI [0.22, 0.54], t(118) = 3.45, p = 0.0008, exploratory robustness check included."
    )
    psych_paper = PaperBundle(
        paper_id="osf:ab12c",
        source_repo="osf",
        title="Behavioral Priming Replication",
        published_year=2024,
        primary_profile=FieldProfile.PSYCHOLOGY_SOCIAL,
        abstract=text,
        full_text=text,
        sections={"methods": text, "results": text},
        artifacts=ArtifactManifest(
            links=extract_artifact_links(text),
            code_files=["analysis.R"],
            data_files=["responses.csv"],
        ),
    )
    psych_report = evaluate_stage0_to_3(psych_paper, profile=FieldProfile.PSYCHOLOGY_SOCIAL)
    ml_report = evaluate_stage0_to_3(psych_paper, profile=FieldProfile.ML_CS)

    by_id = {res.gate_id: res for res in psych_report.gate_results}
    assert by_id["G1.10_PSYCH_PAP_ADHERENCE"].status == GateStatus.PASS
    assert by_id["G3.10_ECON_ROBUSTNESS_SPECIFICATION"].status == GateStatus.PASS
    # Core comparable score agrees across profile switches on core gates
    assert psych_report.core_composite_score == ml_report.core_composite_score


def test_economics_overlay_and_repframe_cli_export(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "master.do").write_text("set seed 12345\nreg y x, cluster(id)\n")
    (tmp_path / "README.md").write_text("Run `stata-mp -b do code/master.do`.\n")
    (tmp_path / "panel.dta").write_bytes(b"mockdta")

    text = (
        "Difference-in-differences estimation on state panel (n = 500) using clustered standard "
        "errors, fixed effects, and placebo test robustness checks (coef = -0.14, 95% CI "
        "[-0.22, -0.06], p = 0.001). OpenICPSR package https://doi.org/10.3886/E199999V1."
    )
    econ_paper = PaperBundle(
        paper_id="icpsr:199999",
        source_repo="icpsr",
        title="Tax Incidence Empirical Analysis",
        published_year=2024,
        primary_profile=FieldProfile.ECONOMICS,
        abstract=text,
        full_text=text,
        sections={"methods": text, "results": text},
        artifacts=ArtifactManifest(
            links=extract_artifact_links(text),
            code_files=["code/master.do"],
            data_files=["panel.dta"],
            manifest_files=["renv.lock"],
            readme_files=["README.md"],
        ),
    )
    report = evaluate_stage0_to_3(econ_paper, workdir=tmp_path)
    by_id = {res.gate_id: res for res in report.gate_results}
    assert by_id["G2.9_STATA_R_PACKAGE_VALID"].status == GateStatus.PASS
    assert by_id["G3.10_ECON_ROBUSTNESS_SPECIFICATION"].status == GateStatus.PASS

    manifest = export_repframe_manifest(econ_paper, report, workdir=tmp_path)
    assert manifest["schema_version"] == "repframe.manifest.v1"
    assert manifest["entrypoints"] == ["code/master.do"]

    bundle_file = tmp_path / "econ_bundle.json"
    bundle_file.write_text(econ_paper.model_dump_json(indent=2), encoding="utf-8")
    repframe_out = tmp_path / "repframe.json"
    report_out = tmp_path / "report.json"

    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "audit",
            str(bundle_file),
            "--workdir",
            str(tmp_path),
            "--profile",
            "economics",
            "--output",
            str(report_out),
            "--repframe",
            str(repframe_out),
        ],
    )
    assert res.exit_code == 0, res.output
    exported = json.loads(repframe_out.read_text(encoding="utf-8"))
    assert exported["profile"] == "economics"
    assert exported["core_composite_score"] is not None

    # Silence unused import lint while asserting execute runner supports crossfield gates
    _ = evaluate_stage0_to_5_with_context


def test_calibration_regressions_non_reg_osf_and_blocked_remote_manifest() -> None:
    non_reg_paper = PaperBundle(
        paper_id="osf:67pcn",
        source_repo="osf",
        title="Exploratory Graph Analysis Method Study",
        published_year=2026,
        primary_profile=FieldProfile.PSYCHOLOGY_SOCIAL,
        abstract=(
            "We evaluate psychometric reliability across simulation datasets "
            "(n = 200, accuracy = 0.90)."
        ),
        full_text="Simulation dataset (n = 200, accuracy = 0.90).",
        artifacts=ArtifactManifest(
            links=[
                ArtifactLink(
                    url="https://osf.io/67pcn/",
                    kind="data",
                    host="osf",
                    status=ArtifactStatus.RESOLVED,
                )
            ]
        ),
        metadata={"is_registration": False},
    )
    non_reg_report = evaluate_stage0_to_3(non_reg_paper)
    non_reg_by_id = {res.gate_id: res for res in non_reg_report.gate_results}
    assert non_reg_by_id["G1.7_PREREGISTRATION_DISCLOSED"].status == GateStatus.FAIL
    assert (
        eval_g1_10_psych_pap_adherence(EvaluationContext(paper=non_reg_paper)).status
        == GateStatus.FAIL
    )

    oversized_dv = PaperBundle(
        paper_id="dataverse:doi:10.7910/DVN/BIG",
        source_repo="dataverse",
        title="Oversized Remote Dataset",
        published_year=2024,
        primary_profile=FieldProfile.ECONOMICS,
        abstract="Empirical panel dataset (n = 5000, p = 0.01).",
        full_text="Empirical panel dataset (n = 5000, p = 0.01).",
        artifacts=ArtifactManifest(
            links=[
                ArtifactLink(
                    url="https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/BIG",
                    kind="data",
                    host="dataverse",
                    status=ArtifactStatus.BLOCKED_DATA_SIZE,
                )
            ],
            data_files=["large_matrix.tab"],
        ),
    )
    dv_report = evaluate_stage0_to_3(oversized_dv)
    dv_by_id = {res.gate_id: res for res in dv_report.gate_results}
    assert dv_by_id["G1.2_DATA_ACCESSIBLE"].status == GateStatus.BLOCKED_DATA_SIZE
