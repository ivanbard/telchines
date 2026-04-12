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

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
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


def _set_model_policy(project_root: Path, model_policy: dict[str, object]) -> None:
    config_path = project_root / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"] = model_policy
    write_json(config_path, payload)


def _write_local_provider(project_root: Path) -> None:
    provider_script = project_root / "tools" / "local_provider.py"
    provider_script.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

payload = json.loads(sys.stdin.read())
target_file = payload["files"][0]
target_path = Path(target_file)
original = target_path.read_text(encoding="utf-8")
candidate = original.replace("count <= 4'd0", "count <= 4'd0;")
response = {
    "status": "proposed",
    "file_path": target_file,
    "candidate_content": candidate,
    "explanation": "Local command fix added the missing semicolon.",
    "evidence_paths": ["docs/spec.md"],
}
sys.stdout.write(json.dumps(response))
""",
        encoding="utf-8",
    )


def _write_local_sva_provider(project_root: Path, *, invalid: bool = False) -> None:
    provider_script = project_root / "tools" / "local_sva_provider.py"
    candidate_content = """module uart_rx_assertions(
  input logic clk,
  input logic rst_n,
  input logic serial_i,
  input logic start_seen
);

property p_start_seen_after_start_bit;
  @(posedge clk) disable iff (!rst_n) (!serial_i) |=> start_seen;
endproperty

assert property (p_start_seen_after_start_bit);

endmodule

bind uart_rx uart_rx_assertions uart_rx_assertions_i(
  .clk(clk),
  .rst_n(rst_n),
  .serial_i(serial_i),
  .start_seen(start_seen)
);
"""
    if invalid:
        candidate_content = """module uart_rx_assertions(
  input logic clk,
  input logic rst_n,
  input logic serial_i,
  input logic start_seen
);

property p_start_seen_after_start_bit;
  @(posedge clk) disable iff (!rst_n) (!serial_i) |=> start_seen;

assert property (p_start_seen_after_start_bit);
"""
    provider_script.write_text(
        f"""from __future__ import annotations
import json
import sys

payload = json.loads(sys.stdin.read())
response = {{
    "status": "proposed",
    "file_path": payload["output_file"],
    "candidate_content": {candidate_content!r},
    "explanation": "Generated a UART receiver start-bit assertion.",
    "evidence_paths": [payload["spec"]["path"], payload["rtl"]["path"]],
    "properties": [
        {{
            "name": "p_start_seen_after_start_bit",
            "summary": "Checks that a detected start bit leads to start_seen on the next cycle.",
            "rationale": "Grounded in the UART receiver start-bit requirement.",
            "source_citation": payload["spec"]["path"],
        }}
    ],
}}
sys.stdout.write(json.dumps(response))
""",
        encoding="utf-8",
    )


def _remote_model_policy(base_url: str, api_key_env: str) -> dict[str, object]:
    return {
        "default_provider_by_capability": {"repair": "mock-remote"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["repair"]},
            "mock-remote": {
                "kind": "openai_compatible",
                "capabilities": ["repair"],
                "base_url": base_url,
                "model": "mock-model",
                "api_key_env": api_key_env,
                "timeout_seconds": 5,
            },
        },
    }


def _local_model_policy(command: str, *args: str) -> dict[str, object]:
    return {
        "default_provider_by_capability": {"repair": "local-test"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["repair"]},
            "local-test": {
                "kind": "local_command",
                "capabilities": ["repair"],
                "command": command,
                "args": list(args),
                "timeout_seconds": 5,
            },
        },
    }


def _generation_model_policy(command: str, *args: str) -> dict[str, object]:
    return {
        "default_provider_by_capability": {"repair": "heuristic", "generation": "sva-local"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["repair", "generation"]},
            "sva-local": {
                "kind": "local_command",
                "capabilities": ["generation"],
                "command": command,
                "args": list(args),
                "timeout_seconds": 5,
            },
        },
    }


def test_cli_index_retrieve_and_repair(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
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
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
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
    assert payload["waveform_count"] == 1
    assert payload["clusters"][0]["waveform_evidence"]


def test_cli_triage_human_and_ci_formats(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    human = runner.invoke(app, ["triage", "--logs", "logs/regressions", "--format", "human"])
    assert human.exit_code == 0
    assert "likely cause:" in human.stdout.lower()
    assert "waveforms:" in human.stdout.lower()
    ci = runner.invoke(app, ["triage", "--logs", "logs/regressions", "--format", "ci"])
    assert ci.exit_code == 0
    payload = json.loads(ci.stdout)
    assert payload["status"] == "needs_attention"
    assert payload["clusters"][0]["evidence"]
    assert payload["clusters"][0]["waveforms"]


def test_cli_triage_accepts_multiple_log_paths(retrieval_corpus_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(retrieval_corpus_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(
        app,
        [
            "triage",
            "--logs",
            "logs/regressions",
            "--logs",
            "logs/regressions/nested/run_b.out",
            "--format",
            "ci",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cluster_count"] == 2
    assert any(cluster["signature"] == "SV_UNKNOWN_IDENTIFIER" for cluster in payload["clusters"])


def test_cli_waveform_commands(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    show_result = runner.invoke(app, ["waveforms", "show", "logs/regressions/uart_rx_trace.vcd"])
    assert show_result.exit_code == 0
    show_payload = json.loads(show_result.stdout)
    assert show_payload["format"] == "vcd"

    signals_result = runner.invoke(app, ["waveforms", "signals", "logs/regressions/uart_rx_trace.vcd", "--filter", "start"])
    assert signals_result.exit_code == 0
    signals_payload = json.loads(signals_result.stdout)
    assert any(item["name"] == "start_seen" for item in signals_payload["signals"])

    inspect_result = runner.invoke(
        app,
        ["waveforms", "inspect", "logs/regressions/uart_rx_trace.vcd", "--signal", "start_seen", "--window", "4"],
    )
    assert inspect_result.exit_code == 0
    inspect_payload = json.loads(inspect_result.stdout)
    assert inspect_payload["signal_name"] == "start_seen"
    assert inspect_payload["transitions"]


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
        _set_model_policy(
            sample_project,
            _remote_model_policy(f"http://127.0.0.1:{server.server_address[1]}", "TELCHINES_TEST_API_KEY"),
        )
        monkeypatch.setenv("TELCHINES_TEST_API_KEY", "test-token")
        monkeypatch.chdir(sample_project)
        monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
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
    _set_model_policy(sample_project, _remote_model_policy("http://127.0.0.1:9", "MISSING_TEST_API_KEY"))
    monkeypatch.delenv("MISSING_TEST_API_KEY", raising=False)
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])
    assert repair_result.exit_code == 2
    assert "provider error" in repair_result.stderr


def test_cli_repair_with_local_command_provider(sample_project: Path, monkeypatch) -> None:
    _write_local_provider(sample_project)
    _set_model_policy(sample_project, _local_model_policy(sys.executable, "tools/local_provider.py"))
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv", "--apply"])
    assert repair_result.exit_code == 0
    payload = json.loads(repair_result.stdout)
    assert payload["provider"] == "local-test"
    assert payload["validation_status"] == "passed"
    assert payload["proposal_explanation"] == "Local command fix added the missing semicolon."


def test_cli_reports_policy_block_for_remote_provider(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "local"
    payload["project"]["model_policy"] = _remote_model_policy("http://127.0.0.1:9", "TELCHINES_TEST_API_KEY")
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])
    assert repair_result.exit_code == 2
    assert "blocked by policy" in repair_result.stderr


def test_cli_reports_policy_block_for_no_egress(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["no_egress"] = True
    payload["project"]["model_policy"] = _remote_model_policy("http://127.0.0.1:9", "TELCHINES_TEST_API_KEY")
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])
    assert repair_result.exit_code == 2
    assert "no_egress=true" in repair_result.stderr


def test_cli_reports_policy_block_for_local_provider(sample_project: Path, monkeypatch) -> None:
    _write_local_provider(sample_project)
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "remote"
    payload["project"]["model_policy"] = _local_model_policy(sys.executable, "tools/local_provider.py")
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])
    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])
    assert repair_result.exit_code == 2
    assert "blocked by policy" in repair_result.stderr


def test_cli_lists_providers(sample_project: Path, monkeypatch) -> None:
    _write_local_provider(sample_project)
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "local"
    payload["project"]["model_policy"] = {
        "default_provider_by_capability": {"repair": "heuristic"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["repair"]},
            "local-test": {
                "kind": "local_command",
                "capabilities": ["repair"],
                "command": sys.executable,
                "args": ["tools/local_provider.py"],
                "timeout_seconds": 5,
            },
            "remote-test": {
                "kind": "openai_compatible",
                "capabilities": ["repair"],
                "base_url": "http://127.0.0.1:9",
                "model": "mock-model",
                "api_key_env": "TELCHINES_TEST_API_KEY",
                "timeout_seconds": 5,
            },
        },
    }
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    provider_payload = json.loads(result.stdout)
    assert provider_payload["default_provider_by_capability"]["repair"] == "heuristic"
    remote = next(item for item in provider_payload["providers"] if item["name"] == "remote-test")
    local = next(item for item in provider_payload["providers"] if item["name"] == "local-test")
    assert remote["allowed"] is False
    assert "blocks remote providers" in remote["blocked_reason"]
    assert local["allowed"] is True


def test_cli_lists_adapters(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["adapters", "list", "--category", "formal"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["adapters"][0]["name"] == "symbiyosys"
    assert payload["adapters"][0]["category"] == "formal"

    simulation_result = runner.invoke(app, ["adapters", "list", "--category", "simulation"])
    assert simulation_result.exit_code == 0
    simulation_payload = json.loads(simulation_result.stdout)
    assert any(item["name"] == "slang" and item["enabled"] for item in simulation_payload["adapters"])


def test_cli_gen_sva_with_local_command_provider(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "sva-local"
    assert payload["status"] == "validated"
    assert payload["validation_status"] == "passed"
    assert payload["artifact_path"].endswith("uart_rx_assertions.sv")
    assert payload["property_summaries"][0]["name"] == "p_start_seen_after_start_bit"
    artifact_path = sample_project / payload["artifact_path"]
    assert artifact_path.exists()
    assert "assert property" in artifact_path.read_text(encoding="utf-8")


def test_cli_gen_sva_reports_validation_failure(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project, invalid=True)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected"
    assert payload["validation_status"] == "failed"
    assert "validation failed" in payload["validation_summary"].lower()


def test_cli_enters_shell_by_default(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, [], input="/exit\n")
    assert result.exit_code == 0
    assert "interactive shell" in result.stdout.lower()
    assert "leaving telchines shell" in result.stdout.lower()


def test_cli_shell_routes_plain_text_and_slash_commands(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, [], input="/providers\nshow my providers\ntriage the regression logs\n/exit\n")
    assert result.exit_code == 0
    assert result.stdout.lower().count("default providers") >= 2
    assert "produced 2 cluster(s)" in result.stdout


def test_cli_shell_supports_explicit_shell_subcommand(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["shell"], input="/pwd\n/exit\n")
    assert result.exit_code == 0
    assert str(sample_project) in result.stdout


def test_cli_shell_help_still_works_in_plain_mode(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    monkeypatch.setenv("TELCHINES_PLAIN_SHELL", "1")
    result = runner.invoke(app, [], input="/help\n/exit\n")
    assert result.exit_code == 0
    assert "Telchines Shell Commands" in result.stdout
    assert "leaving telchines shell" in result.stdout.lower()


def test_cli_shell_supports_gen_sva(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, [], input="/gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv\n/exit\n")
    assert result.exit_code == 0
    assert "Spec-to-SVA Result" in result.stdout
    assert "uart_rx_assertions.sv" in result.stdout


def test_cli_shell_supports_waveform_commands(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(
        app,
        [],
        input="/waveforms show logs/regressions/uart_rx_trace.vcd\n/waveforms inspect logs/regressions/uart_rx_trace.vcd --signal start_seen\n/exit\n",
    )
    assert result.exit_code == 0
    assert "Waveform Summary" in result.stdout
    assert "Waveform Inspect" in result.stdout
