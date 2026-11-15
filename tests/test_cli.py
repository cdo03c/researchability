"""Tests for the researchability CLI and package version metadata."""

from importlib.metadata import version

from typer.testing import CliRunner

from researchability import __version__
from researchability.cli import app

runner = CliRunner()


def test_package_version_is_0_0_0() -> None:
    assert __version__ == "0.0.0"
    assert version("researchability") == "0.0.0"


def test_cli_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("gates", "ingest", "score", "batch", "report"):
        assert cmd in result.stdout


def test_cli_ingest_help_lists_repositories() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "arxiv" in result.stdout
    assert "dataverse" in result.stdout


def test_cli_gates_profile_filtering() -> None:
    ml_res = runner.invoke(app, ["gates", "--profile", "ml_cs"])
    assert ml_res.exit_code == 0
    assert "G0.1_EMPIRICAL_CLAIM" in ml_res.stdout
    assert "G2.6_BIO_REFERENCE_PINNED" not in ml_res.stdout

    bio_res = runner.invoke(app, ["gates", "--profile", "bioinformatics"])
    assert bio_res.exit_code == 0
    assert "G2.6_BIO_REFERENCE_PINNED" in bio_res.stdout
