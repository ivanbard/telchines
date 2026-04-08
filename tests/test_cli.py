from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from typer.testing import CliRunner

from telchines.adapters.base import ToolAdapter
from telchines.cli import app
from telchines.utils import read_json, write_json

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
    assert payload["replay_artifacts"]["request_artifact"]
    fixed_text = (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8")
    assert "count <= 4'd0;" in fixed_text
    task_files = sorted((sample_project / ".tel" / "tasks").glob("*.json"))
    assert task_files
    task_payload = read_json(task_files[-1])
    assert task_payload["metadata"]["request_artifact"]
    assert task_payload["metadata"]["response_artifact"]
    assert task_payload["metadata"]["replay_artifact"]


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
    assert payload["clusters"][0]["evidence_hits"]


def test_cli_triage_human_and_ci_formats(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    human = runner.invoke(app, ["triage", "--logs", "logs/regressions", "--format", "human"])
    assert human.exit_code == 0
    assert "likely cause:" in human.stdout.lower()
    ci = runner.invoke(app, ["triage", "--logs", "logs/regressions", "--format", "ci"])
    assert ci.exit_code == 0
    payload = json.loads(ci.stdout)
    assert payload["status"] == "needs_attention"
    assert payload["clusters"][0]["evidence"]


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


def test_cli_repair_with_openai_compatible_provider(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length).decode("utf-8")
            request_payload = json.loads(body)
            user_payload = json.loads(request_payload["messages"][1]["content"])
            target_file = user_payload["files"][0]
            target_path = sample_project / target_file
            original = target_path.read_text(encoding="utf-8")
            candidate = original.replace("count <= 4'd0", "count <= 4'd0;")
            response = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "proposed",
                                    "file_path": target_file,
                                    "candidate_content": candidate,
                                    "explanation": "Model-backed fix added the missing semicolon.",
                                    "evidence_paths": ["docs/spec.md"],
                                }
                            )
                        }
                    }
                ]
            }
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload["project"]["model_policy"] = {
            "repair_provider": "mock-remote",
            "providers": {
                "heuristic": {"kind": "heuristic"},
                "mock-remote": {
                    "kind": "openai_compatible",
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                    "model": "mock-model",
                    "api_key_env": "TELCHINES_TEST_API_KEY",
                    "timeout_seconds": 5
                }
            }
        }
        write_json(config_path, payload)
        monkeypatch.setenv("TELCHINES_TEST_API_KEY", "test-token")
        monkeypatch.chdir(sample_project)
        monkeypatch.setattr("telchines.cli.AdapterRegistry", FixtureRegistry)
        runner.invoke(app, ["index"])
        repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv", "--apply"])
        assert repair_result.exit_code == 0
        result_payload = json.loads(repair_result.stdout)
        assert result_payload["provider"] == "mock-remote"
        assert result_payload["validation_status"] == "passed"
        assert result_payload["proposal_explanation"] == "Model-backed fix added the missing semicolon."
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_reports_provider_error_when_api_key_missing(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"] = {
        "repair_provider": "mock-remote",
        "providers": {
            "heuristic": {"kind": "heuristic"},
            "mock-remote": {
                "kind": "openai_compatible",
                "base_url": "http://127.0.0.1:9",
                "model": "mock-model",
                "api_key_env": "MISSING_TEST_API_KEY",
                "timeout_seconds": 1
            }
        }
    }
    write_json(config_path, payload)
    monkeypatch.delenv("MISSING_TEST_API_KEY", raising=False)
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])
    assert repair_result.exit_code == 2
    assert "provider error" in repair_result.stderr
