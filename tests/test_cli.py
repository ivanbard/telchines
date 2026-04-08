from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from telchines.adapters.base import ToolAdapter
from telchines.cli import app

runner = CliRunner(mix_stderr=False)


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


def test_cli_repair_unknown_identifier(sample_project: Path, monkeypatch) -> None:
    broken = sample_project / "rtl" / "broken_counter.sv"
    broken.write_text(
        """module broken_counter(
  input logic clk,
  input logic rst_n,
  output logic [3:0] count
);

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    count <= 4'd0;
  end else begin
    coutn <= count + 1;
  end
end

endmodule
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv", "--apply"])
    assert repair_result.exit_code == 0
    payload = json.loads(repair_result.stdout)
    assert payload["validation_status"] == "passed"
    fixed_text = broken.read_text(encoding="utf-8")
    assert "coutn" not in fixed_text
    assert "count <= count + 1;" in fixed_text


def test_cli_triage(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["triage", "--logs", "logs/regressions"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cluster_count"] == 2


def test_cli_reports_project_config_error(work_root: Path, monkeypatch) -> None:
    monkeypatch.chdir(work_root)
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 2
    assert "no Telchines project found" in result.stderr


def test_cli_reports_unknown_adapter(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["repair", "--tool", "missing", "--file", "rtl/broken_counter.sv"])
    assert result.exit_code == 2
    assert "unknown adapter" in result.stderr


def test_cli_entrypoints_install_and_help(work_root: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    assert scripts["tel"] == "telchines.cli:app"
    assert scripts["telchines"] == "telchines.cli:app"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    result = subprocess.run(
        [sys.executable, "-m", "telchines", "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "Telchines CLI" in result.stdout
