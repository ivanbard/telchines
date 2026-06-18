from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from telchines import __version__
from telchines.cli import app

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


def test_package_version_matches_pyproject() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_cli_reports_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_release_files_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expected = [
        repo_root / "LICENSE",
        repo_root / "CHANGELOG.md",
        repo_root / "CONTRIBUTING.md",
        repo_root / "SECURITY.md",
        repo_root / "docs" / "quickstart.md",
        repo_root / "docs" / "providers.md",
        repo_root / "docs" / "local-llms.md",
        repo_root / "docs" / "generated-artifacts.md",
        repo_root / "docs" / "adapters.md",
        repo_root / "docs" / "external-retrieval-policy.md",
        repo_root / "docs" / "evaluation.md",
        repo_root / "docs" / "compatibility.md",
        repo_root / "docs" / "release-checklist.md",
        repo_root / "examples" / "providers" / "local_command_provider.py",
        repo_root / "scripts" / "tool_smoke.py",
    ]
    for path in expected:
        assert path.exists(), path
