"""Tests for the researchability CLI."""

from typer.testing import CliRunner

from researchability.cli import app

runner = CliRunner()


def test_cli_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("gates", "score", "batch", "report"):
        assert cmd in result.stdout


def test_cli_gates_profile_filtering() -> None:
    ml_res = runner.invoke(app, ["gates", "--profile", "ml_cs"])
    assert ml_res.exit_code == 0
    assert "G0.1_EMPIRICAL_CLAIM" in ml_res.stdout
    assert "G2.6_BIO_REFERENCE_PINNED" not in ml_res.stdout

    bio_res = runner.invoke(app, ["gates", "--profile", "bioinformatics"])
    assert bio_res.exit_code == 0
    assert "G2.6_BIO_REFERENCE_PINNED" in bio_res.stdout
