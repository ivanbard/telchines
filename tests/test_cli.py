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

from telchines.adapters.base import AdapterExecution, ToolAdapter
from telchines.cli import app
from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
from telchines.run_store import RunStore
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


class FixtureSvaValidationAdapter(ToolAdapter):
    name = "fixture-sva"
    kind = "simulator"
    category = "simulation"
    supported_workflows = ("generation_validation",)

    def is_available(self) -> bool:
        return True

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return [sys.executable, "-c", "print('fixture sva validation passed')", *files]

    def run(self, run_id: str, project_root: Path, files: list[str], artifacts_dir: Path, extra_args: list[str] | None = None) -> AdapterExecution:
        log_path = artifacts_dir / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fixture sva validation passed\n", encoding="utf-8")
        return AdapterExecution(
            command=self.build_command(project_root, files, extra_args),
            cwd=str(project_root),
            exit_code=0,
            stdout="fixture sva validation passed\n",
            stderr="",
            log_path=str(log_path),
            started_at="2026-04-13T00:00:00+00:00",
            finished_at="2026-04-13T00:00:00+00:00",
            observations=[],
            summary="fixture sva validation passed",
            artifacts={"log_path": str(log_path)},
            result={"status": "passed", "validation_mode": "fixture"},
        )


class FixtureSvaRegistry:
    def get(self, name: str) -> FixtureSvaValidationAdapter:
        assert name == "fixture-sva"
        return FixtureSvaValidationAdapter()


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


def _write_agent_retry_provider(project_root: Path, *, mode: str = "retry") -> None:
    provider_script = project_root / "tools" / "agent_retry_provider.py"
    provider_script.write_text(
        f"""from __future__ import annotations
import json
import sys
from pathlib import Path

payload = json.loads(sys.stdin.read())
target_file = payload["files"][0]
target_path = Path(target_file)
original = target_path.read_text(encoding="utf-8")
previous_attempts = payload.get("previous_attempts", [])
mode = {mode!r}
if mode == "malformed":
    sys.stdout.write("not json")
    raise SystemExit(0)
if mode == "always_bad":
    candidate = original
elif mode == "first_good" or previous_attempts:
    candidate = original.replace("count <= 4'd0", "count <= 4'd0;")
else:
    candidate = original
response = {{
    "status": "proposed",
    "file_path": target_file,
    "candidate_content": candidate,
    "explanation": "Agent retry provider used validation feedback.",
    "evidence_paths": ["docs/spec.md"],
}}
sys.stdout.write(json.dumps(response))
""",
        encoding="utf-8",
    )


def _write_local_check_provider(project_root: Path, *, exit_code: int = 0) -> None:
    provider_script = project_root / "tools" / "local_check_provider.py"
    provider_script.write_text(
        f"""from __future__ import annotations
import json
import sys

payload = json.loads(sys.stdin.read())
if {exit_code} != 0:
    sys.stderr.write("local check failed")
    raise SystemExit({exit_code})
sys.stdout.write("provider log line\\n" + json.dumps({{"status": "ok", "workflow_type": payload.get("workflow_type")}}))
""",
        encoding="utf-8",
    )


def _write_local_sva_provider(project_root: Path, *, invalid: bool = False, bind_signal: str = "start_seen") -> None:
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
  .start_seen(%s)
);
""" % bind_signal
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


def _agent_runtime_model_policy(command: str, *args: str, max_iterations: int = 2) -> dict[str, object]:
    return {
        "default_provider_by_capability": {"repair": "agent-repair", "generation": "heuristic"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["generation"]},
            "local-base": {
                "kind": "local_command",
                "capabilities": ["repair"],
                "command": command,
                "args": list(args),
                "timeout_seconds": 5,
            },
            "agent-repair": {
                "kind": "agent_runtime",
                "runtime": "langgraph",
                "base_provider": "local-base",
                "capabilities": ["repair"],
                "max_iterations": max_iterations,
                "timeout_seconds": 10,
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


def _write_coverage_report(project_root: Path, name: str = "coverage.json") -> Path:
    report_path = project_root / "cov" / name
    write_json(
        report_path,
        {
            "tool": "fixture_cov",
            "generated_at": "2026-04-15T00:00:00+00:00",
            "design": "uart_rx",
            "focus_paths": ["rtl/uart_rx.sv", "docs/uart.md"],
            "items": [
                {
                    "item_id": "rx_start_bit_bin",
                    "module": "uart_rx",
                    "metric": "functional",
                    "name": "start_bit_seen",
                    "hits": 0,
                    "goal": 2,
                    "detail": "Start bit stimulus bin remains uncovered.",
                },
                {
                    "item_id": "rx_start_checker",
                    "module": "uart_rx",
                    "metric": "assertion",
                    "name": "start_bit_assertion",
                    "hits": 0,
                    "goal": 1,
                    "detail": "Checker coverage for the start bit assertion is still empty.",
                },
            ],
        },
    )
    return report_path


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


def test_cli_index_status_and_clean(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    status_before = runner.invoke(app, ["index", "status"])
    assert status_before.exit_code == 0
    before_payload = json.loads(status_before.stdout)
    assert before_payload["status"] == "stale"

    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0
    assert "indexed" in result.stdout

    status_after = runner.invoke(app, ["index", "status"])
    assert status_after.exit_code == 0
    after_payload = json.loads(status_after.stdout)
    assert after_payload["status"] == "fresh"

    clean = runner.invoke(app, ["index", "clean"])
    assert clean.exit_code == 0
    clean_payload = json.loads(clean.stdout)
    assert clean_payload["removed_count"] == 2
    assert not (sample_project / ".tel" / "index").exists()


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


def test_cli_runs_replay_requires_confirmation(sample_project: Path, monkeypatch) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    marker = sample_project / "replayed.txt"
    run = VerificationRun(
        run_id="run_replay_confirm",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="validator", name="fixture"),
        inputs={"files": []},
        status="failed",
        started_at="2026-04-13T00:00:00+00:00",
        replay_command=[sys.executable, "-c", "from pathlib import Path; Path('replayed.txt').write_text('ok', encoding='utf-8')"],
    )
    store.save_run(run)
    monkeypatch.chdir(sample_project)

    dry_run = runner.invoke(app, ["runs", "replay", "run_replay_confirm"])
    assert dry_run.exit_code == 1
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["status"] == "confirmation_required"
    assert not marker.exists()

    executed = runner.invoke(app, ["runs", "replay", "run_replay_confirm", "--yes"])
    assert executed.exit_code == 0
    executed_payload = json.loads(executed.stdout)
    assert executed_payload["status"] == "executed"
    assert executed_payload["exit_code"] == 0
    assert marker.read_text(encoding="utf-8") == "ok"


def test_cli_runs_doctor_reports_corrupt_run_records(sample_project: Path, monkeypatch) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    store.save_run(
        VerificationRun(
            run_id="run_good",
            project_id=config.project.project_id,
            commit_sha="workspace",
            workflow_type="compile_repair",
            tool=ToolReference(kind="simulator", name="verilator"),
            inputs={"files": ["rtl/broken_counter.sv"]},
            status="passed",
            started_at="2026-04-07T00:00:00+00:00",
        )
    )
    (store.runs_dir / "run_corrupt.json").write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(sample_project)

    doctor = runner.invoke(app, ["runs", "doctor"])
    assert doctor.exit_code == 1
    payload = json.loads(doctor.stdout)
    assert payload["status"] == "warning"
    assert payload["run_count"] == 1
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["path"] == "runs/run_corrupt.json"

    listed = runner.invoke(app, ["runs", "list"])
    assert listed.exit_code == 0
    runs = json.loads(listed.stdout)
    assert [run["run_id"] for run in runs] == ["run_good"]


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


def test_cli_repair_with_agent_runtime_provider_first_try(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="first_good")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py"))
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "agent-repair"
    assert payload["validation_status"] == "passed"
    response_payload = read_json(Path(payload["replay_artifacts"]["response_artifact"]))
    agent_runtime = response_payload["agent_runtime"]
    assert agent_runtime["base_provider"] == "local-base"
    assert agent_runtime["final_status"] == "validated"
    assert [step["step"] for step in agent_runtime["steps"]].count("validate_patch") == 1


def test_cli_repair_with_agent_runtime_retries_after_validation_feedback(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="retry")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py", max_iterations=2))
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "agent-repair"
    assert payload["validation_status"] == "passed"
    response_payload = read_json(Path(payload["replay_artifacts"]["response_artifact"]))
    steps = response_payload["agent_runtime"]["steps"]
    assert [step["status"] for step in steps if step["step"] == "validate_patch"] == ["failed", "passed"]
    second_request = next(step["request"] for step in steps if step["step"] == "propose_patch" and step["attempt"] == 2)
    assert second_request["previous_attempts"][0]["validation_status"] == "failed"


def test_cli_agent_repair_is_review_gated_and_records_evidence(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="retry")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py", max_iterations=2))
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])

    result = runner.invoke(
        app,
        [
            "agent",
            "fix the broken counter compile failure",
            "--tool",
            "fixture",
            "--file",
            "rtl/broken_counter.sv",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["workflow_type"] == "compile_repair"
    assert payload["status"] == "review_required"
    assert payload["review_gate"]["required"] is True
    assert payload["result"]["validation_status"] == "passed"
    assert payload["evidence"]["patch_id"]
    assert payload["evidence"]["validation_run_id"]
    assert payload["replay_artifacts"]["replay_artifact"]
    assert [step["step"] for step in payload["steps"]] == [
        "retrieve_context",
        "run_adapter_check",
        "generate_or_repair_candidate",
        "validate_candidate",
    ]
    assert "count <= 4'd0;" not in (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8")
    replay_payload = read_json(Path(payload["replay_artifacts"]["replay_artifact"]))
    assert replay_payload["workflow_type"] == "compile_repair"
    response_payload = read_json(Path(replay_payload["response_artifact"]))
    assert response_payload["task_id"] == payload["task_id"]


def test_cli_repair_with_agent_runtime_returns_no_patch_when_budget_exhausted(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="always_bad")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py", max_iterations=2))
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "agent-repair"
    assert payload["patch_id"] is None
    assert payload["validation_run_id"] is None
    task_payload = read_json(sorted((sample_project / ".tel" / "tasks").glob("*.json"))[-1])
    response_payload = read_json(Path(task_payload["metadata"]["response_artifact"]))
    agent_runtime = response_payload["agent_runtime"]
    assert agent_runtime["final_status"] == "no_patch"
    assert [step["status"] for step in agent_runtime["steps"] if step["step"] == "validate_patch"] == ["failed", "failed"]


def test_cli_repair_with_agent_runtime_captures_malformed_provider_json(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="malformed")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py"))
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["patch_id"] is None
    task_payload = read_json(sorted((sample_project / ".tel" / "tasks").glob("*.json"))[-1])
    response_payload = read_json(Path(task_payload["metadata"]["response_artifact"]))
    agent_runtime = response_payload["agent_runtime"]
    assert agent_runtime["final_status"] == "no_patch"
    assert "JSON" in agent_runtime["final_error"]


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


def test_cli_reports_policy_block_for_agent_runtime_base_provider(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project)
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "remote"
    payload["project"]["model_policy"] = _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py")
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])

    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])

    assert repair_result.exit_code == 2
    assert "base provider local-base is blocked by policy" in repair_result.stderr


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


def test_cli_checks_heuristic_provider(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "check", "heuristic"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["providers"][0]["checks"]["transport"]["mode"] == "builtin"


def test_cli_checks_local_command_provider(sample_project: Path, monkeypatch) -> None:
    _write_local_check_provider(sample_project)
    _set_model_policy(sample_project, _local_model_policy(sys.executable, "tools/local_check_provider.py"))
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "check", "local-test"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["providers"][0]["checks"]["transport"]["parsed_keys"] == ["status", "workflow_type"]


def test_cli_checks_agent_runtime_provider(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project)
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py"))
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["providers", "check", "agent-repair"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    transport = payload["providers"][0]["checks"]["transport"]
    assert transport["mode"] == "agent_runtime"
    assert transport["runtime"] == "langgraph"
    assert transport["base_provider"] == "local-base"


def test_cli_checks_local_command_provider_failure(sample_project: Path, monkeypatch) -> None:
    _write_local_check_provider(sample_project, exit_code=3)
    _set_model_policy(sample_project, _local_model_policy(sys.executable, "tools/local_check_provider.py"))
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "check", "local-test"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert "exit code 3" in payload["providers"][0]["summary"]


def test_cli_checks_openai_compatible_provider(sample_project: Path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            _ = self.rfile.read(length)
            response = {"choices": [{"message": {"content": "```json\n{\"status\":\"ok\"}\n```"}}]}
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
        _set_model_policy(sample_project, _remote_model_policy(f"http://127.0.0.1:{server.server_address[1]}", "TELCHINES_TEST_API_KEY"))
        monkeypatch.setenv("TELCHINES_TEST_API_KEY", "test-token")
        monkeypatch.chdir(sample_project)
        result = runner.invoke(app, ["providers", "check", "mock-remote"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["providers"][0]["checks"]["transport"]["mode"] == "openai_compatible"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_checks_provider_policy_block(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "local"
    payload["project"]["model_policy"] = _remote_model_policy("http://127.0.0.1:9", "TELCHINES_TEST_API_KEY")
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "check", "mock-remote"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["providers"][0]["status"] == "blocked"


def _set_generation_config(project_root: Path, generation: dict[str, object]) -> None:
    config_path = project_root / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["generation"] = generation
    write_json(config_path, payload)


def test_cli_checks_adapters_reports_missing_binary(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.operations.shutil.which", lambda _: None)
    result = runner.invoke(app, ["adapters", "check", "iverilog"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["adapters"][0]["status"] == "missing"
    assert payload["adapters"][0]["missing_binaries"] == ["iverilog", "vvp"]


def test_cli_doctor_privacy_reports_local_command_risks(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["project"]["model_policy"] = _local_model_policy(sys.executable, "tools/local_provider.py")
    payload["project"]["model_policy"]["providers"]["local-test"]["env"] = {"TELCHINES_API_KEY": "literal-secret"}
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["doctor", "privacy"])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["status"] == "warning"
    assert any("local_command" in item["summary"] for item in report["risks"])
    assert any("TELCHINES_API_KEY" in item["summary"] for item in report["risks"])


def test_cli_artifacts_purge_dry_run_and_apply(sample_project: Path, monkeypatch) -> None:
    artifact = sample_project / ".tel" / "artifacts" / "generated.txt"
    task_artifact = sample_project / ".tel" / "task-artifacts" / "request.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    task_artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("generated", encoding="utf-8")
    task_artifact.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(sample_project)

    dry_run = runner.invoke(app, ["artifacts", "purge"])
    assert dry_run.exit_code == 0
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["dry_run"] is True
    assert dry_payload["file_count"] == 2
    assert artifact.exists()

    purged = runner.invoke(app, ["artifacts", "purge", "--yes"])
    assert purged.exit_code == 0
    purge_payload = json.loads(purged.stdout)
    assert purge_payload["status"] == "purged"
    assert not artifact.exists()
    assert not task_artifact.exists()
    assert (sample_project / ".tel" / "artifacts").is_dir()
    assert (sample_project / ".tel" / "task-artifacts").is_dir()


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
    _set_generation_config(sample_project, {"sva": {"validation_adapters": []}})
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "sva-local"
    assert payload["status"] == "validated"
    assert payload["validation_status"] == "passed"
    assert payload["validation_mode"] == "builtin_structural"
    assert payload["validation_limitations"]
    assert payload["artifact_path"].endswith("uart_rx_assertions.sv")
    assert payload["property_summaries"][0]["name"] == "p_start_seen_after_start_bit"
    artifact_path = sample_project / payload["artifact_path"]
    assert artifact_path.exists()
    assert "assert property" in artifact_path.read_text(encoding="utf-8")


def test_cli_gen_sva_reports_validation_failure(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project, invalid=True)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    _set_generation_config(sample_project, {"sva": {"validation_adapters": []}})
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected"
    assert payload["validation_status"] == "failed"
    assert "validation failed" in payload["validation_summary"].lower()


def test_cli_gen_sva_rejects_unknown_bind_signal(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project, bind_signal="missing_signal")
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    _set_generation_config(sample_project, {"sva": {"validation_adapters": []}})
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "rejected"
    assert payload["validation_status"] == "failed"
    assert "unknown DUT signal `missing_signal`" in payload["validation_summary"]


def test_cli_gen_sva_uses_available_adapter_validation(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    _set_generation_config(sample_project, {"sva": {"validation_adapters": ["fixture-sva"]}})
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["adapters"].append("fixture-sva")
    write_json(config_path, payload)
    monkeypatch.setattr("telchines.config.SUPPORTED_ADAPTERS", {"verilator", "iverilog", "slang", "verible", "symbiyosys", "fixture", "fixture-sva"})
    monkeypatch.setattr("telchines.workflows.gen_sva.AdapterRegistry", FixtureSvaRegistry)
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "validated"
    assert payload["validation_status"] == "passed"
    assert payload["validation_mode"] == "adapter_backed"
    run_payload = read_json(sample_project / ".tel" / "runs" / f"{payload['validation_run_id']}.json")
    assert run_payload["tool_result"]["validator"] == "fixture-sva"


def test_cli_gen_cocotb_with_heuristic_provider(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(
        app,
        [
            "gen-cocotb",
            "--dut",
            "rtl/uart_rx.sv",
            "--spec",
            "docs/uart.md",
            "--intent",
            "smoke the start-bit path",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "heuristic"
    assert payload["status"] == "validated"
    assert payload["validation_status"] == "passed"
    assert payload["validation_mode"] == "python_syntax_plus_structure"
    assert payload["validation_limitations"]
    assert payload["top_module"] == "uart_rx"
    assert payload["artifact_path"].endswith("test_uart_rx.py")
    assert payload["manifest_path"].endswith("uart_rx_cocotb_manifest.json")
    assert payload["run_id"] is not None
    assert payload["assumptions"]
    assert any(item["name"] == "serial_i" for item in payload["ports"])
    artifact_path = sample_project / payload["artifact_path"]
    manifest_path = sample_project / payload["manifest_path"]
    assert artifact_path.exists()
    assert manifest_path.exists()
    assert "@cocotb.test()" in artifact_path.read_text(encoding="utf-8")
    manifest = read_json(manifest_path)
    assert manifest["validation"]["mode"] == "python_syntax_plus_structure"

    unchanged = runner.invoke(app, ["artifacts", "review", payload["candidate_id"]])
    assert unchanged.exit_code == 0
    review_payload = json.loads(unchanged.stdout)
    assert review_payload["status"] == "unchanged"
    assert review_payload["generated_file"] == payload["artifact_path"]

    artifact_path.write_text(artifact_path.read_text(encoding="utf-8") + "\n# human review note\n", encoding="utf-8")
    modified = runner.invoke(app, ["artifacts", "review", payload["validation_run_id"], "--max-diff-lines", "20"])
    assert modified.exit_code == 0
    modified_payload = json.loads(modified.stdout)
    assert modified_payload["status"] == "modified"
    assert "+# human review note" in modified_payload["diff"]


def test_cli_gen_cocotb_uses_generation_conventions(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["generation"] = {
        "cocotb": {
            "output_dir": ".tel/artifacts/custom-cocotb",
            "test_file_template": "tb_{module}.py",
            "manifest_file_template": "{module}_manifest.json",
        }
    }
    write_json(config_path, payload)
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-cocotb", "--dut", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["artifact_path"].endswith(".tel/artifacts/custom-cocotb/tb_uart_rx.py")
    assert payload["manifest_path"].endswith(".tel/artifacts/custom-cocotb/uart_rx_manifest.json")


def test_cli_coverage_plan(sample_project: Path, monkeypatch) -> None:
    _write_coverage_report(sample_project)
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(
        app,
        [
            "coverage-plan",
            "--report",
            "cov/coverage.json",
            "--rtl",
            "rtl/uart_rx.sv",
            "--spec",
            "docs/uart.md",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["recommendation_count"] == 2
    assert payload["recommendations"][0]["classification"] == "missing_stimulus"
    assert payload["recommendations"][1]["classification"] == "missing_checker"
    assert payload["recommendations"][0]["evidence_citations"]


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


def test_cli_shell_supports_agent_command(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="first_good")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py"))
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])

    result = runner.invoke(
        app,
        [],
        input='/agent "fix the broken counter compile failure" --tool fixture --file rtl/broken_counter.sv\n/exit\n',
    )

    assert result.exit_code == 0
    assert "Agent Result" in result.stdout
    assert "status: review_required" in result.stdout
    assert "validation status: passed" in result.stdout


def test_cli_shell_supports_plain_mode_flag(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["shell", "--plain"], input="/pwd\n/exit\n")
    assert result.exit_code == 0
    assert "mode: plain" in result.stdout
    assert str(sample_project) in result.stdout


def test_cli_plain_shell_subprocess_smoke(sample_project: Path) -> None:
    env = os.environ.copy()
    src_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "telchines", "shell", "--plain"],
        cwd=sample_project,
        input="/pwd\n/providers\n/repair --tool fixture --file\n/exit\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0
    assert "mode: plain" in result.stdout
    assert str(sample_project) in result.stdout
    assert "Default Providers" in result.stdout
    assert "error: --file requires a value" in result.stdout
    assert "leaving Telchines shell" in result.stdout


def test_cli_plain_shell_subprocess_exits_on_eof(sample_project: Path) -> None:
    env = os.environ.copy()
    src_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "telchines", "shell", "--plain"],
        cwd=sample_project,
        input="/pwd\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0
    assert "mode: plain" in result.stdout
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


def test_cli_shell_supports_gen_cocotb(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(
        app,
        [],
        input="/gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md --intent \"smoke start bit\"\n/exit\n",
    )
    assert result.exit_code == 0
    assert "DUT-to-Cocotb Result" in result.stdout
    assert "test_uart_rx.py" in result.stdout


def test_cli_shell_supports_coverage_plan(sample_project: Path, monkeypatch) -> None:
    _write_coverage_report(sample_project)
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, [], input="/coverage-plan --report cov/coverage.json --rtl rtl/uart_rx.sv --spec docs/uart.md\n/exit\n")
    assert result.exit_code == 0
    assert "Coverage Plan" in result.stdout
    assert "missing_stimulus" in result.stdout


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
