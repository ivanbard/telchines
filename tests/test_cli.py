from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from telchines.adapters.base import ToolAdapter
from telchines.cli import app

runner = CliRunner()


class FixtureAdapter(ToolAdapter):
    name = "fixture"
    kind = "linter"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return [sys.executable, "tools/fixture_lint.py", *files]


class FixtureRegistry:
    def get(self, name: str) -> FixtureAdapter:
        assert name == "fixture"
        return FixtureAdapter()


def test_cli_index_retrieve_and_repair(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.AdapterRegistry", FixtureRegistry)
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv", "--apply"])
    assert repair_result.exit_code == 0
    payload = json.loads(repair_result.stdout)
    assert payload["patch_id"] is not None
    assert payload["validation_status"] == "passed"
    fixed_text = (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8")
    assert "count <= 4'd0;" in fixed_text


def test_cli_triage(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["triage", "--logs", "logs/regressions"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cluster_count"] == 2
