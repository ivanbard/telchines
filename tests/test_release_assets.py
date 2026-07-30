from __future__ import annotations

import re
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


def test_pyproject_uses_setuptools_compatible_license_table() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["license"] == {"text": "MIT"}


def test_release_version_surfaces_match() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"version-v{__version__}-" in readme
    assert f"Version {__version__}" in readme
    assert re.search(rf"^## {re.escape(__version__)} - (?:Unreleased|\d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.MULTILINE)


def test_checkout_and_packaged_benchmarks_match() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    checkout_root = repo_root / "benchmarks"
    packaged_root = repo_root / "src" / "telchines" / "benchmarks"

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
        }

    assert snapshot(checkout_root) == snapshot(packaged_root)


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
        repo_root / "docs" / "provider-capability-study.md",
        repo_root / "docs" / "provider-matrices" / "anthropic.json",
        repo_root / "docs" / "provider-matrices" / "openrouter.json",
        repo_root / "docs" / "provider-matrices" / "local_command.json",
        repo_root / "docs" / "generated-artifacts.md",
        repo_root / "docs" / "adapters.md",
        repo_root / "docs" / "external-retrieval-policy.md",
        repo_root / "docs" / "evaluation.md",
        repo_root / "docs" / "compatibility.md",
        repo_root / "docs" / "release-checklist.md",
        repo_root / "docs" / "assets" / "telchines-logo.png",
        repo_root / "examples" / "providers" / "local_command_provider.py",
        repo_root / "scripts" / "tool_smoke.py",
        repo_root / "scripts" / "provider_capability_study.py",
    ]
    for path in expected:
        assert path.exists(), path


def test_current_docs_do_not_hard_code_benchmark_case_counts() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    historical_report = repo_root / "docs" / "telchines-agentic-verification-evaluation.md"
    checked_paths = [
        path
        for path in [repo_root / "README.md", *sorted((repo_root / "docs").glob("*.md"))]
        if path != historical_report
    ]
    stale_count_pattern = re.compile(r"\b\d+\s+bundled benchmark cases\b|\b\d+\s+benchmark cases\b|\b\d+\s+cases\b|benchmarks-[0-9]+%20cases")

    offenders = [str(path.relative_to(repo_root)) for path in checked_paths if stale_count_pattern.search(path.read_text(encoding="utf-8"))]

    assert offenders == []


def test_p5_docs_describe_current_ux_contracts() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    evaluation = (repo_root / "docs" / "evaluation.md").read_text(encoding="utf-8")
    providers = (repo_root / "docs" / "providers.md").read_text(encoding="utf-8")
    generated = (repo_root / "docs" / "generated-artifacts.md").read_text(encoding="utf-8")
    quickstart = (repo_root / "docs" / "quickstart.md").read_text(encoding="utf-8")

    assert "project_context" in readme
    assert "report_persisted" in evaluation
    assert "tel providers setup" in providers
    assert "did not prove" in generated
    assert "Task artifacts keep prompts" in quickstart
    assert "tel doctor privacy" in generated
