"""Tests for Zenodo/OSF downloads, .xlsx/.dta/.rds/.sav profiling, Stage 4 workflows & I4R."""

from __future__ import annotations

import gzip
import io
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import researchability.cli as cli_mod
from researchability.batch import BatchConfig, BatchSweepRunner, BatchTarget
from researchability.calibration import evaluate_calibration, load_calibration_dataset
from researchability.connectors import ArtifactResolver, OsfConnector
from researchability.gates.base import EvaluationContext
from researchability.gates.stage4_execution import (
    _discover_main_commands,
    eval_g4_4_analysis_zero_touch,
    eval_g4_5_analysis_repaired,
)
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    FieldProfile,
    GateStatus,
    PaperBundle,
)
from researchability.profiler import profile_data_file
from researchability.sandbox import CommandResult, DockerSandboxRunner, SandboxConfig


def _build_minimal_xlsx(dest: Path) -> None:
    """Build a valid multi-type .xlsx ZIP containing sharedStrings, inlineStr, and numbers."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>outcome</t></si>"
            "<si><t>email</t></si>"
            "<si><t>empty_col</t></si>"
            "<si><t>alice@example.edu</t></si>"
            "</sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c>'
            "</row>"
            '<row r="2"><c r="A2"><v>0</v></c><c r="B2" t="s"><v>3</v></c></row>'
            '<row r="3"><c r="A3"><v>0</v></c><c r="B3" t="inlineStr"><is><t>ok</t></is></c></row>'
            '<row r="4"><c r="A4"><v>0</v></c></row>'
            '<row r="5"><c r="A5"><v>12.5</v></c></row>'
            '<row r="6"><c r="A6"><v>-999</v></c></row>'
            "</sheetData>"
            "</worksheet>",
        )
    dest.write_bytes(buf.getvalue())


def _build_stata_118(dest: Path) -> None:
    """Build a valid Stata 118 (<stata_dta>) binary file with LSF numeric and fixed str columns."""
    varnames = b"".join(
        name.encode("utf-8").ljust(129, b"\x00") for name in ("outcome", "age", "tag")
    )
    vtypes = struct.pack("<3H", 65530, 65527, 4)  # double (8B), int (2B), str4 (4B)
    rows = [
        (0.0, 25, b"a\x00\x00\x00"),
        (0.0, 30, b"b\x00\x00\x00"),
        (0.0, 35, b"c\x00\x00\x00"),
        (0.0, 40, b"d\x00\x00\x00"),
        (10.0, 45, b"e\x00\x00\x00"),
        (-999.0, 50, b"f\x00\x00\x00"),
    ]
    data_blob = b"".join(struct.pack("<dh4s", o, a, t) for o, a, t in rows)
    payload = b"".join(
        [
            b"<stata_dta><header>",
            b"<release>118</release>",
            b"<byteorder>LSF</byteorder>",
            b"<K>",
            struct.pack("<H", 3),
            b"</K><N>",
            struct.pack("<Q", len(rows)),
            b"</N>",
            b"<label>\x00</label><timestamp>\x00</timestamp>",
            b"</header>",
            b"<variable_types>",
            vtypes,
            b"</variable_types>",
            b"<varnames>",
            varnames,
            b"</varnames>",
            b"<data>",
            data_blob,
            b"</data></stata_dta>",
        ]
    )
    dest.write_bytes(payload)


def _build_stata_114_header(dest: Path, *, nobs: int = 42) -> None:
    """Construct a valid classic Stata format-114 binary header."""
    header = bytearray(4 + 2 + 4 + 81 + 18)
    header[0] = 114
    header[1] = 2  # LSF little-endian
    header[2] = 1
    struct.pack_into("<H", header, 4, 2)
    struct.pack_into("<I", header, 6, nobs)
    typlist = bytes([255, 253])
    varlist = b"id".ljust(33, b"\x00") + b"income".ljust(33, b"\x00")
    dest.write_bytes(bytes(header) + typlist + varlist)


def test_artifact_resolver_downloads_zenodo_and_osf_files(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://zenodo.org/api/records/555111":
            return httpx.Response(
                200,
                json={
                    "metadata": {"access_right": "open"},
                    "files": [
                        {
                            "key": "analysis.R",
                            "size": 12,
                            "links": {"self": "https://zenodo.org/api/files/555111/analysis.R"},
                        },
                        {
                            "key": "../escape.csv",
                            "size": 10,
                            "links": {"self": "https://zenodo.org/api/files/555111/escape.csv"},
                        },
                    ],
                },
            )
        if url == "https://zenodo.org/api/files/555111/analysis.R":
            return httpx.Response(200, content=b"print('zen')\n")
        if url == "https://api.osf.io/v2/nodes/pub99/":
            return httpx.Response(200, json={"data": {"attributes": {"public": True}}})
        if url == "https://api.osf.io/v2/nodes/pub99/files/osfstorage/":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "kind": "file",
                                "name": "survey.csv",
                                "materialized_path": "/data/survey.csv",
                                "size": 11,
                            },
                            "links": {"download": "https://files.osf.io/v1/resources/pub99/survey"},
                        }
                    ]
                },
            )
        if url == "https://files.osf.io/v1/resources/pub99/survey":
            return httpx.Response(200, content=b"x,y\n1,2\n")
        return httpx.Response(404)

    manifest = ArtifactManifest(
        links=[
            ArtifactLink(url="https://zenodo.org/records/555111", host="zenodo", kind="data"),
            ArtifactLink(url="https://osf.io/pub99/", host="osf", kind="data"),
        ]
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with ArtifactResolver(http_client=http) as resolver:
        resolved = resolver.resolve_manifest(manifest, workdir=tmp_path)

    assert (tmp_path / "zenodo_files" / "555111" / "analysis.R").is_file()
    assert not (tmp_path / "escape.csv").exists()
    assert (tmp_path / "osf_files" / "pub99" / "data" / "survey.csv").is_file()
    assert "analysis.R" in resolved.code_files
    assert "data/survey.csv" in resolved.data_files

    paper = PaperBundle(paper_id="demo", source_repo="test", title="T", artifacts=resolved)
    ctx = EvaluationContext(paper=paper, workdir=tmp_path)
    assert len(ctx.resolve_relative_files(["analysis.R", "data/survey.csv"])) == 2


def test_osf_connector_download_and_cli_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.osf.io/v2/nodes/xyz12/":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "title": "Public Psych Study",
                            "description": "Experiment study",
                            "date_created": "2024-02-01T00:00:00Z",
                            "public": True,
                            "registration": False,
                        }
                    }
                },
            )
        if url == "https://api.osf.io/v2/nodes/xyz12/files/osfstorage/":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "kind": "file",
                                "name": "run.R",
                                "materialized_path": "/run.R",
                                "size": 10,
                            },
                            "links": {"download": "https://files.osf.io/v1/resources/xyz12/run.R"},
                        }
                    ]
                },
            )
        if url == "https://files.osf.io/v1/resources/xyz12/run.R":
            return httpx.Response(200, content=b"cat('ok')\n")
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with OsfConnector(http_client=http) as conn:
        bundle = conn.fetch_project("xyz12", workdir=tmp_path, download_files=True)

    assert "run.R" in bundle.artifacts.code_files
    assert (tmp_path / "osf_files" / "xyz12" / "run.R").is_file()

    # Verify CLI --workdir + --download passes through cleanly
    monkeypatch.setattr(cli_mod, "OsfConnector", lambda: OsfConnector(http_client=http))
    cli_dir = tmp_path / "cli_dl"
    res = CliRunner().invoke(
        cli_mod.app,
        ["ingest", "osf", "xyz12", "-w", str(cli_dir), "--download"],
    )
    assert res.exit_code == 0
    assert (cli_dir / "osf_files" / "xyz12" / "run.R").is_file()


def test_data_profiler_xlsx_stata_and_r_spss(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "table.xlsx"
    _build_minimal_xlsx(xlsx_path)
    x_prof = profile_data_file(xlsx_path)
    assert x_prof.parseable is True
    assert x_prof.format == "xlsx"
    assert x_prof.row_count == 5
    assert x_prof.columns == ["outcome", "email", "empty_col"]
    assert "outcome" in x_prof.sentinel_columns
    assert "empty_col" in x_prof.empty_columns
    assert x_prof.zero_mass_columns["outcome"] == 0.6
    assert any("email" in item.lower() for item in x_prof.pii_findings)

    dta118 = tmp_path / "panel.dta"
    _build_stata_118(dta118)
    d_prof = profile_data_file(dta118)
    assert d_prof.parseable is True
    assert d_prof.format == "dta"
    assert d_prof.row_count == 6
    assert d_prof.column_count == 3
    assert d_prof.columns == ["outcome", "age", "tag"]
    assert "outcome" in d_prof.sentinel_columns
    assert d_prof.zero_mass_columns["outcome"] > 0.6
    assert d_prof.numeric_means["age"] == pytest.approx(37.5)

    dta114 = tmp_path / "legacy.dta"
    _build_stata_114_header(dta114, nobs=42)
    l_prof = profile_data_file(dta114)
    assert l_prof.parseable is True
    assert l_prof.row_count == 42
    assert l_prof.columns == ["id", "income"]

    good_rds = tmp_path / "ok.rds"
    good_rds.write_bytes(gzip.compress(b"X\n\x00\x00\x00\x03"))
    bad_rds = tmp_path / "corrupt.rds"
    bad_rds.write_bytes(b"NOT_AN_RDS")
    good_sav = tmp_path / "ok.sav"
    good_sav.write_bytes(b"$FL2@(#) SPSS DATA FILE")

    assert profile_data_file(good_rds).parseable is True
    assert profile_data_file(bad_rds).parseable is False
    assert profile_data_file(good_sav).parseable is True


def test_stage4_sequential_readme_commands_and_notebook_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Pipeline\n1. Run `python step1.py`\n2. Run `python step2.py`\n",
        encoding="utf-8",
    )
    (tmp_path / "step1.py").write_text(
        "from pathlib import Path\nPath('inter.txt').write_text('42')\n",
        encoding="utf-8",
    )
    # step2 deliberately references an author absolute path to trigger G4.5 repair
    (tmp_path / "step2.py").write_text(
        "from pathlib import Path\n"
        "val = Path('/Users/author/proj/inter.txt').read_text()\n"
        "Path('metrics.json').write_text('{\"acc\": 0.91}')\n",
        encoding="utf-8",
    )
    (tmp_path / "explore.ipynb").write_text('{"cells": []}\n', encoding="utf-8")

    paper = PaperBundle(
        paper_id="seq:1",
        source_repo="arxiv",
        title="Sequential Pipeline",
        primary_profile=FieldProfile.ML_CS,
        artifacts=ArtifactManifest(
            code_files=["step1.py", "step2.py", "explore.ipynb"],
            readme_files=["README.md"],
        ),
    )

    df_nb = DockerSandboxRunner().render_dockerfile(paper, tmp_path)
    assert "nbconvert" in df_nb

    def fake_runner(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        workspace = cwd
        for idx, tok in enumerate(args):
            if tok == "-v" and idx + 1 < len(args) and args[idx + 1].endswith(":/workspace"):
                workspace = Path(args[idx + 1][: -len(":/workspace")])
        assert workspace is not None
        py_idx = args.index("python")
        cmd = [sys.executable, *args[py_idx + 1 :]]
        proc = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, check=False)
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            command=args,
        )

    sandbox = DockerSandboxRunner(config=SandboxConfig(), runner=fake_runner)
    ctx = EvaluationContext(paper=paper, workdir=tmp_path)
    ctx.extracted_claims["sandbox_runner"] = sandbox

    cmds = _discover_main_commands(ctx, tmp_path)
    assert cmds == [["python", "step1.py"], ["python", "step2.py"]]

    g44 = eval_g4_4_analysis_zero_touch(ctx)
    assert g44.status == GateStatus.FAIL

    g45 = eval_g4_5_analysis_repaired(ctx)
    assert g45.status == GateStatus.PASS
    assert "step2.py" in g45.evidence[0].source


def test_batch_runner_supports_osf_and_icpsr(tmp_path: Path) -> None:
    class StubOsf:
        def fetch_project(
            self,
            ident: str,
            *,
            workdir: Path | None = None,
            download_files: bool = False,
        ) -> PaperBundle:
            return PaperBundle(
                paper_id=f"osf:{ident}",
                source_repo="osf",
                title="OSF Study",
                primary_profile=FieldProfile.PSYCHOLOGY_SOCIAL,
            )

        def close(self) -> None:
            return None

    class StubIcpsr:
        def fetch_project(self, ident: str) -> PaperBundle:
            return PaperBundle(
                paper_id=f"icpsr:{ident}",
                source_repo="icpsr",
                title="ICPSR Study",
                primary_profile=FieldProfile.ECONOMICS,
            )

        def close(self) -> None:
            return None

    class NoOpResolver:
        def resolve_bundle(self, bundle: PaperBundle, workdir: Path | None = None) -> PaperBundle:
            return bundle

        def close(self) -> None:
            return None

    cfg = BatchConfig(
        db_path=tmp_path / "db.duckdb",
        parquet_dir=None,
        workspace_root=tmp_path / "ws",
    )
    with BatchSweepRunner(
        config=cfg,
        osf=StubOsf(),  # type: ignore[arg-type]
        icpsr=StubIcpsr(),  # type: ignore[arg-type]
        resolver=NoOpResolver(),  # type: ignore[arg-type]
    ) as runner:
        summary = runner.sweep_targets(
            [
                BatchTarget(paper_id="abcde", source_repo="osf"),
                BatchTarget(paper_id="199001", source_repo="icpsr"),
            ]
        )
    assert summary.succeeded == 2
    assert summary.failed == 0


def test_starter_i4r_calibration_dataset() -> None:
    dataset_path = Path("examples/calibration_i4r_starter.jsonl")
    assert dataset_path.is_file()
    samples = load_calibration_dataset(dataset_path)
    assert len(samples) == 6
    report = evaluate_calibration(samples)
    assert report.sample_count == 6
    assert report.scorable_pairs >= 30
    assert report.overall_f1 is not None and report.overall_f1 >= 0.9
    assert report.overall_cohens_kappa is not None and report.overall_cohens_kappa >= 0.8
