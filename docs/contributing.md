# Contributing & Releases

## Local Development Setup

```bash
uv sync --all-extras
uv run pre-commit install
```

## Quality Checks

GitHub Actions CI runs these checks on every push and pull request to `main` and `*-SNAPSHOT` development branches (such as `1.1.0-SNAPSHOT`):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v --cov=researchability --cov-report=term-missing
uv run mkdocs build --strict
```

## Versioning & Changelog Policy

- This repository follows [Semantic Versioning](https://semver.org/) and [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Update `CHANGELOG.md` under `[Unreleased]` for every user-facing change.
- Tagging `vX.Y.Z` triggers `.github/workflows/release.yml`, which verifies the tag matches `pyproject.toml`, runs tests, builds distributions, and publishes a GitHub Release.
