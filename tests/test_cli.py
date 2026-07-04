from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from telchines.adapters.base import AdapterExecution, ToolAdapter
from telchines.cli import app
from telchines.config import ProjectConfig
from telchines.errors import ConfigError
from telchines.models import ToolReference, VerificationRun
from telchines.operations import _normalize_provider_setup_capabilities, _provider_setup_config, privacy_report, setup_provider
from telchines.run_store import RunStore
from telchines.utils import read_json, write_json

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


PROVIDER_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{0,12}", fullmatch=True).filter(lambda value: value != "heuristic")
CAPABILITY_LIST = st.lists(st.sampled_from(["repair", "generation"]), min_size=1, max_size=8)
INVALID_CAPABILITY = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=16,
).filter(lambda value: value.strip() not in {"repair", "generation"})
MODEL_NAME = st.from_regex(r"[A-Za-z0-9_.:/-]{1,32}", fullmatch=True)
ENV_NAME = st.from_regex(r"[A-Z][A-Z0-9_]{0,24}", fullmatch=True)
HTTP_URL = st.from_regex(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9_./-]+)?", fullmatch=True)
POSITIVE_TIMEOUT = st.integers(min_value=1, max_value=300)
NON_POSITIVE_TIMEOUT = st.integers(max_value=0)


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
if payload.get("workflow_type") == "provider_check":
    sys.stdout.write(json.dumps({"status": "ok", "workflow_type": "provider_check"}))
    raise SystemExit(0)
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
if payload.get("workflow_type") == "provider_check":
    sys.stdout.write(json.dumps({{"status": "ok", "workflow_type": "provider_check"}}))
    raise SystemExit(0)
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


def _write_local_sva_provider(project_root: Path, *, invalid: bool = False, bind_signal: str = "start_seen", valid_after_feedback: bool = False) -> None:
    provider_script = project_root / "tools" / "local_sva_provider.py"
    valid_candidate_content = """module uart_rx_assertions(
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
    candidate_content = valid_candidate_content
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
invalid = {invalid!r} and not ({valid_after_feedback!r} and payload.get("previous_attempts"))
response = {{
    "status": "proposed",
    "file_path": payload["output_file"],
    "candidate_content": ({valid_candidate_content!r} if not invalid else {candidate_content!r}),
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


def _write_local_cocotb_provider(project_root: Path) -> None:
    provider_script = project_root / "tools" / "local_cocotb_provider.py"
    provider_script.write_text(
        """from __future__ import annotations
import json
import sys

payload = json.loads(sys.stdin.read())
has_feedback = bool(payload.get("previous_attempts"))
content = '''import cocotb

@cocotb.test()
async def test_smoke(dut):
    dut._log.info("smoke")
'''
if not has_feedback:
    content = '''import cocotb

@cocotb.test()
async def test_smoke(dut):
    dut._log.info("unterminated)
'''
response = {
    "status": "proposed",
    "file_path": payload["default_output_file"],
    "manifest_path": payload["default_manifest_file"],
    "candidate_content": content,
    "explanation": "Generated a retry-aware cocotb smoke scaffold.",
    "evidence_paths": [payload["dut"]["path"]],
    "top_module": payload["dut"]["module_name"],
    "assumptions": payload["inference"]["assumptions"],
    "ports": payload["dut"]["ports"],
}
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


def _anthropic_model_policy(base_url: str, api_key_env: str) -> dict[str, object]:
    return {
        "default_provider_by_capability": {"repair": "mock-anthropic"},
        "providers": {
            "heuristic": {"kind": "heuristic", "capabilities": ["repair"]},
            "mock-anthropic": {
                "kind": "anthropic",
                "capabilities": ["repair"],
                "base_url": base_url,
                "model": "claude-test",
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
    assert payload["status"] == "applied"
    assert payload["workflow_status"] == "applied"
    assert payload["initial_tool_status"] == "failed"
    assert payload["candidate_status"] == "validated"
    assert payload["review_status"] == "applied"
    assert payload["patch_id"] is not None
    assert payload["validation_status"] == "passed"
    assert payload["validation_mode"] == "adapter_replay"
    assert payload["replay_artifacts"]["request_artifact"]
    fixed_text = (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8")
    assert "count <= 4'd0;" in fixed_text
    task_files = sorted((sample_project / ".tel" / "tasks").glob("*.json"))
    assert task_files
    task_payload = read_json(task_files[-1])
    assert task_payload["metadata"]["request_artifact"]
    assert task_payload["metadata"]["response_artifact"]
    assert task_payload["metadata"]["replay_artifact"]


def test_cli_repair_without_apply_is_review_required(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    original_text = (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8")
    runner.invoke(app, ["index"])

    repair_result = runner.invoke(app, ["repair", "--tool", "fixture", "--file", "rtl/broken_counter.sv"])

    assert repair_result.exit_code == 0
    payload = json.loads(repair_result.stdout)
    assert payload["status"] == "review_required"
    assert payload["workflow_status"] == "review_required"
    assert payload["initial_tool_status"] == "failed"
    assert payload["candidate_status"] == "validated"
    assert payload["review_status"] == "pending_review"
    assert payload["validation_status"] == "passed"
    assert payload["validation_mode"] == "adapter_replay"
    assert (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8") == original_text


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
    assert "relevance" in payload["clusters"][0]["waveforms"][0]
    assert "reason" in payload["clusters"][0]["waveforms"][0]
    assert "matched" in human.stdout.lower() or "unrelated" in human.stdout.lower()


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

    missing_result = runner.invoke(
        app,
        ["waveforms", "inspect", "logs/regressions/uart_rx_trace.vcd", "--signal", "rx", "--window", "4"],
    )
    assert missing_result.exit_code != 0
    assert "signal was not found in waveform: rx" in missing_result.stderr
    assert "serial_i" in missing_result.stderr


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


def test_cli_runs_import_manifest(sample_project: Path, monkeypatch) -> None:
    imported_logs = sample_project / "logs" / "imported"
    imported_logs.mkdir(parents=True, exist_ok=True)
    (imported_logs / "run_a.log").write_text("rtl/uart_rx.sv:42: error: timeout waiting for start bit\n", encoding="utf-8")
    manifest = sample_project / "regression_manifest.json"
    write_json(
        manifest,
        {
            "schema_version": "0.1",
            "tool": {"kind": "regression_manager", "name": "nightly"},
            "runs": [
                {
                    "name": "uart_rx_seed_1",
                    "status": "failed",
                    "logs": ["logs/imported/run_a.log"],
                    "artifacts": {"spec": "docs/uart.md"},
                }
            ],
        },
    )
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["runs", "import", "regression_manifest.json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["imported_count"] == 1
    run_id = payload["runs"][0]["run_id"]

    show = runner.invoke(app, ["runs", "show", run_id])
    assert show.exit_code == 0
    shown = json.loads(show.stdout)
    assert shown["workflow_type"] == "regression_import"
    assert shown["tool"]["name"] == "nightly"
    assert shown["artifacts"]["spec"] == "docs/uart.md"

    dry_run = runner.invoke(app, ["runs", "import", "regression_manifest.json", "--dry-run"])
    assert dry_run.exit_code == 0
    assert json.loads(dry_run.stdout)["dry_run"] is True


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
    assert payload["result"]["status"] == "review_required"
    assert payload["result"]["workflow_status"] == "review_required"
    assert payload["result"]["initial_tool_status"] == "failed"
    assert payload["result"]["candidate_status"] == "validated"
    assert payload["result"]["review_status"] == "pending_review"
    assert payload["review_gate"]["required"] is True
    assert payload["result"]["validation_status"] == "passed"
    assert payload["result"]["validation_mode"] == "adapter_replay"
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
    assert payload["status"] == "no_patch"
    assert payload["workflow_status"] == "no_patch"
    assert payload["candidate_status"] == "no_patch"
    assert payload["review_status"] == "not_available"
    assert payload["patch_id"] is None
    assert payload["validation_run_id"] is None
    task_payload = read_json(sorted((sample_project / ".tel" / "tasks").glob("*.json"))[-1])
    response_payload = read_json(Path(task_payload["metadata"]["response_artifact"]))
    agent_runtime = response_payload["agent_runtime"]
    assert agent_runtime["final_status"] == "no_patch"
    assert [step["status"] for step in agent_runtime["steps"] if step["step"] == "validate_patch"] == ["failed", "failed"]


def test_cli_agent_repair_provider_override_is_used(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project, mode="valid")
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py"))
    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.operations.AdapterRegistry", FixtureRegistry)
    runner.invoke(app, ["index"])

    result = runner.invoke(
        app,
        [
            "agent",
            "fix counter",
            "--tool",
            "fixture",
            "--file",
            "rtl/broken_counter.sv",
            "--provider",
            "local-base",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["provider"] == "local-base"
    assert payload["result"]["runtime_mode"] == ""


def test_cli_repair_forwards_compile_context_options(sample_project: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_repair(project_root, **kwargs):  # noqa: ANN001
        captured["project_root"] = project_root
        captured.update(kwargs)
        return {"status": "captured"}

    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.repair_op", fake_repair)

    result = runner.invoke(
        app,
        [
            "repair",
            "--tool",
            "fixture",
            "--filelist",
            "design.f",
            "--include-dir",
            "rtl/include",
            "--define",
            "SIM=1",
            "--top",
            "tb_top",
            "--worklib",
            "work",
            "--adapter-arg",
            "-Wall",
            "--extra-arg",
            "--legacy",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "captured"}
    assert captured["tool"] == "fixture"
    assert captured["files"] == []
    assert captured["filelists"] == ["design.f"]
    assert captured["include_dirs"] == ["rtl/include"]
    assert captured["defines"] == ["SIM=1"]
    assert captured["top_module"] == "tb_top"
    assert captured["work_library"] == "work"
    assert captured["adapter_args"] == ["-Wall"]
    assert captured["extra_arg"] == ["--legacy"]


def test_cli_agent_forwards_compile_context_options(sample_project: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_agent(project_root, task, **kwargs):  # noqa: ANN001
        captured["project_root"] = project_root
        captured["task"] = task
        captured.update(kwargs)
        return {"workflow_type": "compile_repair", "status": "captured"}

    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.agent_op", fake_agent)

    result = runner.invoke(
        app,
        [
            "agent",
            "fix counter",
            "--tool",
            "fixture",
            "--file",
            "rtl/broken_counter.sv",
            "--filelist",
            "design.f",
            "--include-dir",
            "rtl/include",
            "--define",
            "SIM=1",
            "--top",
            "tb_top",
            "--worklib",
            "work",
            "--adapter-arg",
            "-Wall",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "captured"
    assert captured["task"] == "fix counter"
    assert captured["tool"] == "fixture"
    assert captured["files"] == ["rtl/broken_counter.sv"]
    assert captured["filelists"] == ["design.f"]
    assert captured["include_dirs"] == ["rtl/include"]
    assert captured["defines"] == ["SIM=1"]
    assert captured["top_module"] == "tb_top"
    assert captured["work_library"] == "work"
    assert captured["adapter_args"] == ["-Wall"]


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
    payload["project"]["model_policy"] = _remote_model_policy("https://example.invalid/v1", "TELCHINES_TEST_API_KEY")
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
    payload["project"]["model_policy"] = _remote_model_policy("https://example.invalid/v1", "TELCHINES_TEST_API_KEY")
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
                "base_url": "https://example.invalid/v1",
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
    assert remote["network_scope"] == "external_http"
    assert "blocks remote providers" in remote["blocked_reason"]
    assert local["allowed"] is True
    assert local["network_scope"] == "local_process"


def test_cli_checks_heuristic_provider(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "check", "heuristic"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    provider = payload["providers"][0]
    assert provider["model"] == "heuristic"
    assert provider["reasoning_level"] == "auto"
    assert provider["checks"]["transport"]["mode"] == "builtin"
    assert provider["checks"]["transport"]["model"] == "heuristic"


def test_cli_checks_local_command_provider(sample_project: Path, monkeypatch) -> None:
    _write_local_check_provider(sample_project)
    _set_model_policy(sample_project, _local_model_policy(sys.executable, "tools/local_check_provider.py"))
    config_path = sample_project / ".tel" / "config.json"
    config = read_json(config_path)
    config["project"]["model_policy"]["providers"]["local-test"]["model"] = "wrapper-model"
    config["project"]["model_policy"]["providers"]["local-test"]["reasoning_level"] = "medium"
    write_json(config_path, config)
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, ["providers", "check", "local-test"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    provider = payload["providers"][0]
    assert provider["model"] == "wrapper-model"
    assert provider["reasoning_level"] == "medium"
    assert provider["checks"]["transport"]["model"] == "wrapper-model"
    assert provider["checks"]["transport"]["reasoning_level"] == "medium"
    assert provider["checks"]["transport"]["parsed_keys"] == ["status", "workflow_type"]


def test_cli_model_selection_commands_persist_config(sample_project: Path, monkeypatch) -> None:
    _write_local_check_provider(sample_project)
    _set_model_policy(sample_project, _local_model_policy(sys.executable, "tools/local_check_provider.py"))
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["providers", "set-model", "local-test", "wrapper-v2"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["providers", "set-reasoning", "local-test", "high"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["providers", "select", "--capability", "repair", "--provider", "local-test"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["providers", "models", "local-test", "--offline"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    provider = payload["providers"][0]
    assert provider["model"] == "wrapper-v2"
    assert provider["reasoning_level"] == "high"
    config = read_json(sample_project / ".tel" / "config.json")
    assert config["project"]["model_policy"]["providers"]["local-test"]["model"] == "wrapper-v2"
    assert config["project"]["model_policy"]["providers"]["local-test"]["reasoning_level"] == "high"


def test_cli_provider_setup_openai_compatible_uses_env_var_and_selects_defaults(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)

    result = runner.invoke(
        app,
        [
            "providers",
            "setup",
            "openrouter-dev",
            "--kind",
            "openai-compatible",
            "--model",
            "provider/model",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--api-key-env",
            "OPENROUTER_API_KEY",
            "--capability",
            "repair",
            "--capability",
            "generation",
            "--select-defaults",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "updated"
    assert payload["selected_defaults"] == {"generation": "openrouter-dev", "repair": "openrouter-dev"}
    assert "OPENROUTER_API_KEY" in result.stdout
    assert "literal-secret" not in result.stdout
    config = read_json(sample_project / ".tel" / "config.json")
    provider = config["project"]["model_policy"]["providers"]["openrouter-dev"]
    assert provider["kind"] == "openai_compatible"
    assert provider["api_key_env"] == "OPENROUTER_API_KEY"
    assert provider["model"] == "provider/model"
    assert "literal-secret" not in json.dumps(provider)
    assert config["project"]["model_policy"]["default_provider_by_capability"]["repair"] == "openrouter-dev"
    assert config["project"]["model_policy"]["default_provider_by_capability"]["generation"] == "openrouter-dev"


def test_cli_provider_setup_anthropic_and_local_openai_shapes(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)

    anthropic = runner.invoke(
        app,
        [
            "providers",
            "setup",
            "anthropic-dev",
            "--kind",
            "anthropic",
            "--model",
            "claude-sonnet-5",
            "--api-key-env",
            "ANTHROPIC_API_KEY",
        ],
    )
    local = runner.invoke(
        app,
        [
            "providers",
            "setup",
            "local-openai",
            "--kind",
            "local-openai",
            "--model",
            "qwen2.5-coder",
            "--auth",
            "none",
            "--capability",
            "generation",
        ],
    )

    assert anthropic.exit_code == 0
    assert local.exit_code == 0
    config = read_json(sample_project / ".tel" / "config.json")
    anthropic_provider = config["project"]["model_policy"]["providers"]["anthropic-dev"]
    local_provider = config["project"]["model_policy"]["providers"]["local-openai"]
    assert anthropic_provider["kind"] == "anthropic"
    assert anthropic_provider["api_key_env"] == "ANTHROPIC_API_KEY"
    assert anthropic_provider["timeout_seconds"] == 90
    assert local_provider["kind"] == "openai_compatible"
    assert local_provider["auth"] == "none"
    assert "api_key_env" not in local_provider
    assert local_provider["base_url"] == "http://127.0.0.1:11434/v1"
    assert local_provider["capabilities"] == ["generation"]


def test_cli_provider_setup_reports_required_values(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)

    missing_base = runner.invoke(app, ["providers", "setup", "remote", "--kind", "openai-compatible", "--model", "m", "--api-key-env", "KEY"])
    missing_env = runner.invoke(app, ["providers", "setup", "remote", "--kind", "openai-compatible", "--model", "m", "--base-url", "https://example.com/v1"])
    missing_model = runner.invoke(app, ["providers", "setup", "remote", "--kind", "anthropic", "--api-key-env", "KEY"])

    assert missing_base.exit_code == 2
    assert "base_url is required" in missing_base.stderr
    assert missing_env.exit_code == 2
    assert "api_key_env is required" in missing_env.stderr
    assert missing_model.exit_code != 0


@settings(max_examples=50)
@given(capabilities=CAPABILITY_LIST)
def test_provider_setup_capabilities_dedupe_preserving_order(capabilities: list[str]) -> None:
    normalized = _normalize_provider_setup_capabilities(capabilities)
    expected = []
    for capability in capabilities:
        if capability not in expected:
            expected.append(capability)

    assert normalized == expected
    assert _normalize_provider_setup_capabilities(None) == ["repair", "generation"]


@settings(max_examples=50)
@given(invalid=INVALID_CAPABILITY)
def test_provider_setup_capabilities_reject_invalid_values(invalid: str) -> None:
    with pytest.raises(ConfigError, match="capability must be repair or generation"):
        _normalize_provider_setup_capabilities(["repair", invalid])


@settings(max_examples=40)
@given(model=MODEL_NAME, base_url=HTTP_URL, api_key_env=ENV_NAME, timeout=POSITIVE_TIMEOUT)
def test_provider_setup_openai_compatible_config_shape(model: str, base_url: str, api_key_env: str, timeout: int) -> None:
    provider = _provider_setup_config(
        "openai-compatible",
        ["repair", "generation"],
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        auth="bearer",
        timeout_seconds=timeout,
    )

    assert provider["kind"] == "openai_compatible"
    assert provider["base_url"] == base_url
    assert provider["model"] == model
    assert provider["api_key_env"] == api_key_env
    assert provider["auth"] == "bearer"
    assert provider["timeout_seconds"] == timeout


@settings(max_examples=40)
@given(model=MODEL_NAME, base_url=HTTP_URL)
def test_provider_setup_openai_compatible_auth_none_does_not_require_env(model: str, base_url: str) -> None:
    provider = _provider_setup_config(
        "openai-compatible",
        ["generation"],
        model=model,
        base_url=base_url,
        api_key_env=None,
        auth="none",
        timeout_seconds=None,
    )

    assert provider["auth"] == "none"
    assert "api_key_env" not in provider
    assert provider["timeout_seconds"] == 60


@settings(max_examples=40)
@given(model=MODEL_NAME, api_key_env=ENV_NAME, timeout=POSITIVE_TIMEOUT)
def test_provider_setup_anthropic_config_shape(model: str, api_key_env: str, timeout: int) -> None:
    provider = _provider_setup_config(
        "anthropic",
        ["repair"],
        model=model,
        base_url=None,
        api_key_env=api_key_env,
        auth=None,
        timeout_seconds=timeout,
    )

    assert provider["kind"] == "anthropic"
    assert provider["base_url"] == "https://api.anthropic.com/v1"
    assert provider["model"] == model
    assert provider["api_key_env"] == api_key_env
    assert provider["timeout_seconds"] == timeout


@settings(max_examples=40)
@given(model=MODEL_NAME)
def test_provider_setup_local_openai_defaults_to_no_auth(model: str) -> None:
    provider = _provider_setup_config(
        "local-openai",
        ["generation"],
        model=model,
        base_url=None,
        api_key_env=None,
        auth=None,
        timeout_seconds=None,
    )

    assert provider["kind"] == "openai_compatible"
    assert provider["base_url"] == "http://127.0.0.1:11434/v1"
    assert provider["auth"] == "none"
    assert "api_key_env" not in provider
    assert provider["timeout_seconds"] == 60


@settings(max_examples=30)
@given(timeout=NON_POSITIVE_TIMEOUT)
def test_provider_setup_rejects_non_positive_timeouts(timeout: int) -> None:
    with pytest.raises(ConfigError, match="timeout_seconds must be a positive integer"):
        _provider_setup_config(
            "local-openai",
            ["generation"],
            model="local-model",
            base_url=None,
            api_key_env=None,
            auth=None,
            timeout_seconds=timeout,
        )


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(provider_name=PROVIDER_NAME, model=MODEL_NAME, api_key_env=ENV_NAME)
def test_provider_setup_operation_writes_valid_config_without_literal_secrets(
    sample_project: Path,
    provider_name: str,
    model: str,
    api_key_env: str,
) -> None:
    result = setup_provider(
        sample_project,
        provider_name,
        kind="openai-compatible",
        capabilities=["repair", "generation"],
        model=model,
        base_url="https://example.com/v1",
        api_key_env=api_key_env,
        auth="bearer",
        timeout_seconds=30,
        select_defaults=True,
    )

    config = ProjectConfig.load(sample_project)
    provider = config.project.model_policy["providers"][provider_name]
    assert result["status"] == "updated"
    assert provider["api_key_env"] == api_key_env
    assert provider["model"] == model
    assert "literal-secret" not in json.dumps(config.to_dict())
    assert config.default_provider_by_capability()["repair"] == provider_name
    assert config.default_provider_by_capability()["generation"] == provider_name


def test_cli_models_discovers_openai_compatible_models(sample_project: Path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert self.path == "/models"
            assert self.headers["Authorization"] == "Bearer test-token"
            response = {"data": [{"id": "live-model-a"}, {"id": "live-model-b"}]}
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
        result = runner.invoke(app, ["providers", "models", "mock-remote"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        provider = payload["providers"][0]
        assert provider["discovery_status"] == "passed"
        assert provider["model_source"] == "live"
        assert provider["models"][:2] == ["live-model-a", "live-model-b"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_models_falls_back_when_credentials_are_missing(sample_project: Path, monkeypatch) -> None:
    _set_model_policy(sample_project, _remote_model_policy("https://example.invalid/v1", "TELCHINES_TEST_API_KEY"))
    monkeypatch.delenv("TELCHINES_TEST_API_KEY", raising=False)
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["providers", "models", "mock-remote"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    provider = payload["providers"][0]
    assert provider["discovery_status"] == "fallback"
    assert provider["model"] == "mock-model"
    assert "missing credentials" in provider["discovery_error"]


def test_cli_models_handles_openai_discovery_malformed_data(sample_project: Path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            response = {"data": "not-a-list"}
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
        result = runner.invoke(app, ["providers", "models", "mock-remote"])
        assert result.exit_code == 0
        provider = json.loads(result.stdout)["providers"][0]
        assert provider["discovery_status"] == "fallback"
        assert provider["models"][0] == "mock-model"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_models_discovers_anthropic_models(sample_project: Path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert self.path == "/models"
            assert self.headers["x-api-key"] == "test-token"
            assert self.headers["anthropic-version"] == "2023-06-01"
            response = {"data": [{"id": "claude-live-a"}]}
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
        _set_model_policy(sample_project, _anthropic_model_policy(f"http://127.0.0.1:{server.server_address[1]}", "TELCHINES_TEST_API_KEY"))
        monkeypatch.setenv("TELCHINES_TEST_API_KEY", "test-token")
        monkeypatch.chdir(sample_project)
        result = runner.invoke(app, ["providers", "models", "mock-anthropic"])
        assert result.exit_code == 0
        provider = json.loads(result.stdout)["providers"][0]
        assert provider["discovery_status"] == "passed"
        assert provider["model_source"] == "live"
        assert provider["models"][0] == "claude-live-a"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_provider_model_commands_report_config_errors(sample_project: Path, monkeypatch) -> None:
    _write_local_check_provider(sample_project)
    _set_model_policy(sample_project, _local_model_policy(sys.executable, "tools/local_check_provider.py"))
    monkeypatch.chdir(sample_project)

    missing = runner.invoke(app, ["providers", "models", "missing", "--offline"])
    bad_capability = runner.invoke(app, ["providers", "select", "--capability", "simulation", "--provider", "local-test"])
    bad_provider = runner.invoke(app, ["providers", "set-model", "missing", "model"])
    bad_reasoning = runner.invoke(app, ["providers", "set-reasoning", "local-test", "maximum"])

    assert missing.exit_code == 2
    assert "provider missing is not configured" in missing.stderr
    assert bad_capability.exit_code == 2
    assert "capability must be repair or generation" in bad_capability.stderr
    assert bad_provider.exit_code == 2
    assert "provider missing is not configured" in bad_provider.stderr
    assert bad_reasoning.exit_code == 2
    assert "reasoning level must be one of" in bad_reasoning.stderr


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
    assert transport["runtime_mode"] in {"bounded_loop_no_langgraph", "langgraph"}
    assert isinstance(transport["runtime_available"], bool)
    assert transport["runtime_reason"]
    assert transport["base_provider"] == "local-base"
    assert payload["providers"][0]["checks"]["base_provider_transport"]["status"] == "passed"


def test_cli_checks_agent_runtime_provider_offline_skips_base_transport(sample_project: Path, monkeypatch) -> None:
    _write_agent_retry_provider(sample_project)
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/agent_retry_provider.py"))
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["providers", "check", "agent-repair", "--offline"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    checks = payload["providers"][0]["checks"]
    assert checks["transport"]["status"] == "skipped"
    assert "base_provider_transport" not in checks


def test_cli_checks_agent_runtime_provider_fails_when_base_transport_fails(sample_project: Path, monkeypatch) -> None:
    _write_local_check_provider(sample_project, exit_code=3)
    _set_model_policy(sample_project, _agent_runtime_model_policy(sys.executable, "tools/local_check_provider.py"))
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["providers", "check", "agent-repair"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["providers"][0]["checks"]["transport"]["status"] == "passed"
    assert payload["providers"][0]["checks"]["base_provider_transport"]["status"] == "failed"
    assert "base provider local-base check failed" in payload["providers"][0]["summary"]


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
        transport = payload["providers"][0]["checks"]["transport"]
        assert transport["mode"] == "openai_compatible"
        assert transport["auth_mode"] == "bearer"
        assert transport["network_scope"] == "local_http"
        assert payload["providers"][0]["model"] == "mock-model"
        assert payload["providers"][0]["reasoning_level"] == "auto"
        assert "test-token" not in result.stdout
        assert "Authorization" not in result.stdout
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_checks_local_openai_provider_without_auth_in_local_no_egress_mode(sample_project: Path, monkeypatch) -> None:
    seen_headers: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            _ = self.rfile.read(length)
            seen_headers.append({key.lower(): value for key, value in self.headers.items()})
            response = {"choices": [{"message": {"content": "{\"status\":\"ok\"}"}}]}
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
        config_path = sample_project / ".tel" / "config.json"
        payload = read_json(config_path)
        payload["model_mode"] = "local"
        payload["no_egress"] = True
        payload["project"]["model_policy"] = _remote_model_policy(f"http://127.0.0.1:{server.server_address[1]}", "TELCHINES_TEST_API_KEY")
        payload["project"]["model_policy"]["providers"]["mock-remote"]["auth"] = "none"
        write_json(config_path, payload)
        monkeypatch.chdir(sample_project)
        result = runner.invoke(app, ["providers", "check", "mock-remote"])
        assert result.exit_code == 0
        response = json.loads(result.stdout)
        transport = response["providers"][0]["checks"]["transport"]
        assert transport["network_scope"] == "local_http"
        assert transport["auth_mode"] == "none"
        assert "authorization" not in seen_headers[0]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_checks_anthropic_provider(sample_project: Path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            assert self.headers["x-api-key"] == "test-token"
            assert self.headers["anthropic-version"] == "2023-06-01"
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            assert body["model"] == "claude-test"
            response = {"content": [{"type": "text", "text": "{\"status\":\"ok\"}"}]}
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
        _set_model_policy(sample_project, _anthropic_model_policy(f"http://127.0.0.1:{server.server_address[1]}", "TELCHINES_TEST_API_KEY"))
        monkeypatch.setenv("TELCHINES_TEST_API_KEY", "test-token")
        monkeypatch.chdir(sample_project)
        result = runner.invoke(app, ["providers", "check", "mock-anthropic"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        transport = payload["providers"][0]["checks"]["transport"]
        assert transport["mode"] == "anthropic"
        assert transport["network_scope"] == "local_http"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_checks_provider_policy_block(sample_project: Path, monkeypatch) -> None:
    config_path = sample_project / ".tel" / "config.json"
    payload = read_json(config_path)
    payload["model_mode"] = "local"
    payload["project"]["model_policy"] = _remote_model_policy("https://example.invalid/v1", "TELCHINES_TEST_API_KEY")
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
    assert report["cleanup_command"] == "tel artifacts purge"
    assert "proprietary RTL" in report["redaction_scope"]


def test_cli_doctor_privacy_includes_retention_guidance_when_ok(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["doctor", "privacy"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    assert report["retention_guidance"]
    assert any("task artifacts" in item.lower() for item in report["retention_guidance"])
    assert report["remote_context_warning"]


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(provider_name=PROVIDER_NAME, api_key_env=ENV_NAME)
def test_privacy_guidance_is_always_present_and_info_risks_do_not_warn(
    sample_project: Path,
    provider_name: str,
    api_key_env: str,
) -> None:
    model_policy = _remote_model_policy("https://example.com/v1", api_key_env)
    remote_config = model_policy["providers"].pop("mock-remote")
    model_policy["providers"][provider_name] = remote_config
    model_policy["default_provider_by_capability"] = {"repair": provider_name}
    _set_model_policy(sample_project, model_policy)

    report = privacy_report(sample_project)

    assert report["status"] == "ok"
    assert report["retention_guidance"]
    assert report["cleanup_command"] == "tel artifacts purge"
    assert "proprietary RTL" in report["redaction_scope"]
    assert report["remote_context_warning"]
    assert any(item["severity"] == "info" for item in report["risks"])
    assert not any(item["severity"] == "warning" for item in report["risks"])


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    secret_key=st.sampled_from(["API_KEY", "auth_token", "PASSWORD", "client_secret"]),
    secret_value=st.from_regex(r"ZZZSECRET[0-9]{4}", fullmatch=True),
)
def test_privacy_local_command_secret_env_reports_key_not_value(sample_project: Path, secret_key: str, secret_value: str) -> None:
    model_policy = _local_model_policy(sys.executable)
    model_policy["providers"]["local-test"]["env"] = {secret_key: secret_value}
    _set_model_policy(sample_project, model_policy)

    report = privacy_report(sample_project)
    risk_summaries = "\n".join(str(item.get("summary", "")) for item in report["risks"] if isinstance(item, dict))

    assert report["status"] == "warning"
    assert secret_key in risk_summaries
    assert secret_value not in risk_summaries
    assert report["retention_guidance"]


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
    assert payload["validation_mode"] == "structure_only"
    assert payload["validation_limitations"]
    assert payload["artifact_path"].endswith("uart_rx_assertions.sv")
    assert payload["property_summaries"][0]["name"] == "p_start_seen_after_start_bit"
    artifact_path = sample_project / payload["artifact_path"]
    assert artifact_path.exists()
    assert "assert property" in artifact_path.read_text(encoding="utf-8")


def test_cli_gen_sva_forwards_compile_context_options(sample_project: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_gen_sva(project_root, **kwargs):  # noqa: ANN001
        captured["project_root"] = project_root
        captured.update(kwargs)
        return {"status": "captured"}

    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.gen_sva_op", fake_gen_sva)

    result = runner.invoke(
        app,
        [
            "gen-sva",
            "--spec",
            "docs/uart.md",
            "--rtl",
            "rtl/uart_rx.sv",
            "--filelist",
            "design.f",
            "--include-dir",
            "rtl/include",
            "--define",
            "SIM=1",
            "--top",
            "formal_top",
            "--worklib",
            "work",
            "--adapter-arg",
            "--append",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "captured"}
    assert captured["spec"] == Path("docs/uart.md")
    assert captured["rtl"] == Path("rtl/uart_rx.sv")
    assert captured["filelists"] == ["design.f"]
    assert captured["include_dirs"] == ["rtl/include"]
    assert captured["defines"] == ["SIM=1"]
    assert captured["top_module"] == "formal_top"
    assert captured["work_library"] == "work"
    assert captured["adapter_args"] == ["--append"]


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


def test_cli_gen_sva_retries_with_validation_feedback(sample_project: Path, monkeypatch) -> None:
    _write_local_sva_provider(sample_project, invalid=True, valid_after_feedback=True)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_sva_provider.py"))
    _set_generation_config(sample_project, {"sva": {"validation_adapters": [], "max_attempts": 2}})
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "validated"
    assert payload["validation_status"] == "passed"
    assert len(payload["attempts"]) == 2
    assert payload["attempts"][0]["validation_status"] == "failed"
    assert payload["attempts"][1]["validation_status"] == "passed"
    assert len(payload["rejected_candidate_ids"]) == 1
    rejected = runner.invoke(app, ["artifacts", "review", payload["rejected_candidate_ids"][0]])
    assert rejected.exit_code == 0
    assert json.loads(rejected.stdout)["candidate_id"] == payload["rejected_candidate_ids"][0]


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
    assert payload["validation_mode"] == "syntax_plus_structure"
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
    assert manifest["validation"]["mode"] == "syntax_plus_structure"

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


def test_cli_gen_cocotb_forwards_compile_context_options(sample_project: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_gen_cocotb(project_root, **kwargs):  # noqa: ANN001
        captured["project_root"] = project_root
        captured.update(kwargs)
        return {"status": "captured"}

    monkeypatch.chdir(sample_project)
    monkeypatch.setattr("telchines.cli.gen_cocotb_op", fake_gen_cocotb)

    result = runner.invoke(
        app,
        [
            "gen-cocotb",
            "--dut",
            "rtl/uart_rx.sv",
            "--spec",
            "docs/uart.md",
            "--filelist",
            "design.f",
            "--include-dir",
            "rtl/include",
            "--define",
            "SIM=1",
            "--top",
            "uart_rx",
            "--worklib",
            "work",
            "--adapter-arg",
            "-Wall",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "captured"}
    assert captured["dut"] == Path("rtl/uart_rx.sv")
    assert captured["spec"] == Path("docs/uart.md")
    assert captured["filelists"] == ["design.f"]
    assert captured["include_dirs"] == ["rtl/include"]
    assert captured["defines"] == ["SIM=1"]
    assert captured["top_module"] == "uart_rx"
    assert captured["work_library"] == "work"
    assert captured["adapter_args"] == ["-Wall"]


def test_cli_gen_cocotb_missing_dut_reports_input_error(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])

    result = runner.invoke(app, ["gen-cocotb", "--dut", "rtl/does_not_exist.sv"])

    assert result.exit_code == 2
    assert "input error: dut file does not exist: rtl/does_not_exist.sv" in result.stderr
    assert "provider error" not in result.stderr


def test_cli_missing_workflow_inputs_report_input_errors(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])

    cases = [
        (["gen-sva", "--spec", "docs/does_not_exist.md", "--rtl", "rtl/uart_rx.sv"], "input error: spec file does not exist: docs/does_not_exist.md"),
        (["gen-sva", "--spec", "docs/uart.md", "--rtl", "rtl/does_not_exist.sv"], "input error: rtl file does not exist: rtl/does_not_exist.sv"),
        (["coverage-plan", "--report", "cov/does_not_exist.json"], "input error: coverage report does not exist: cov/does_not_exist.json"),
        (["triage", "--logs", "logs/does_not_exist"], "input error: log path does not exist: logs/does_not_exist"),
        (
            ["triage", "--logs", "logs/regressions", "--waveform", "logs/regressions/does_not_exist.vcd"],
            "input error: waveform file does not exist: logs/regressions/does_not_exist.vcd",
        ),
    ]
    for command, message in cases:
        result = runner.invoke(app, command)
        assert result.exit_code == 2
        assert message in result.stderr
        assert "provider error" not in result.stderr


def test_cli_gen_cocotb_retries_with_validation_feedback(sample_project: Path, monkeypatch) -> None:
    _write_local_cocotb_provider(sample_project)
    _set_model_policy(sample_project, _generation_model_policy(sys.executable, "tools/local_cocotb_provider.py"))
    _set_generation_config(sample_project, {"cocotb": {"max_attempts": 2}})
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    result = runner.invoke(app, ["gen-cocotb", "--dut", "rtl/uart_rx.sv"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "validated"
    assert payload["validation_status"] == "passed"
    assert len(payload["attempts"]) == 2
    assert payload["attempts"][0]["validation_status"] == "failed"
    assert payload["attempts"][1]["validation_status"] == "passed"
    assert len(payload["rejected_candidate_ids"]) == 1


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


def test_cli_agent_evidence_workflows_use_success_statuses(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)
    runner.invoke(app, ["index"])
    triage_result = runner.invoke(app, ["agent", "triage the UART regression", "--logs", "logs/regressions"])
    assert triage_result.exit_code == 0
    triage_payload = json.loads(triage_result.stdout)
    assert triage_payload["status"] == "triaged"
    assert triage_payload["result"]["status"] == "triaged"

    _write_coverage_report(sample_project)
    coverage_result = runner.invoke(app, ["agent", "plan coverage closure", "--report", "cov/coverage.json"])
    assert coverage_result.exit_code == 0
    coverage_payload = json.loads(coverage_result.stdout)
    assert coverage_payload["status"] == "planned"
    assert coverage_payload["result"]["status"] == "planned"


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


def test_cli_plain_shell_prints_single_startup_welcome(sample_project: Path, monkeypatch) -> None:
    monkeypatch.chdir(sample_project)

    result = runner.invoke(app, ["shell", "--plain"], input="/exit\n")

    assert result.exit_code == 0
    assert result.stdout.count("Shell ready") == 1
    assert "leaving Telchines shell" in result.stdout


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


def test_openrouter_capability_harness_dry_run_and_missing_key(monkeypatch) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "openrouter_capability_study.py"
    dry_run = subprocess.run([sys.executable, str(script), "--dry-run"], capture_output=True, text=True, check=False)
    assert dry_run.returncode == 0
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["status"] == "dry_run"
    assert any(item["label"] == "agent_repair" for item in dry_payload["commands"])

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    skipped = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert skipped.returncode == 0
    skipped_payload = json.loads(skipped.stdout)
    assert skipped_payload["status"] == "skipped_missing_key"
    assert skipped_payload["missing_env"] == "OPENROUTER_API_KEY"


def test_provider_capability_harness_local_matrix(sample_project: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "provider_capability_study.py"
    matrix = Path(__file__).resolve().parents[1] / "docs" / "provider-matrices" / "local_command.json"
    scratch_root = sample_project.parent / "provider-study"
    dry_run = subprocess.run([sys.executable, str(script), "--matrix", str(matrix), "--dry-run"], capture_output=True, text=True, check=False)
    assert dry_run.returncode == 0
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["matrix"] == "local_command"
    assert any(item["label"] == "gen_cocotb" for item in dry_payload["commands"])

    result = subprocess.run(
        [sys.executable, str(script), "--matrix", str(matrix), "--scratch-root", str(scratch_root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout[result.stdout.rfind("{") :])
    assert payload["status"] == "passed"
    summary = read_json(scratch_root / "local_command" / "local_command_provider_capability_summary.json")
    assert summary["status"] == "passed"
    assert any(item["label"] == "gen_sva" and item["attempt_count"] == 2 for item in summary["results"])
    assert (scratch_root / "local_command" / "local_command_provider_capability_summary.md").exists()


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


def test_cli_shell_supports_runs_import(sample_project: Path, monkeypatch) -> None:
    imported_logs = sample_project / "logs" / "imported"
    imported_logs.mkdir(parents=True, exist_ok=True)
    (imported_logs / "run_a.log").write_text("rtl/uart_rx.sv:42: error: timeout waiting for start bit\n", encoding="utf-8")
    write_json(
        sample_project / "regression_manifest.json",
        {
            "schema_version": "0.1",
            "tool": "shell-regress",
            "runs": [{"name": "seed_1", "status": "failed", "logs": ["logs/imported/run_a.log"]}],
        },
    )
    monkeypatch.chdir(sample_project)
    result = runner.invoke(app, [], input="/runs import regression_manifest.json\n/exit\n")
    assert result.exit_code == 0
    assert "Runs Imported" in result.stdout
    assert "seed_1" in result.stdout
