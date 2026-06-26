from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
from telchines.operations import import_runs, triage
from telchines.run_store import RunStore
from telchines.utils import read_json, write_json


def test_run_store_round_trip(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    run = VerificationRun(
        run_id="run_1",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="simulator", name="verilator"),
        inputs={"files": ["rtl/broken_counter.sv"]},
        status="failed",
        started_at="2026-04-07T00:00:00+00:00",
        tool_result={"status": "failed", "validation_mode": "compile_only", "assumptions": ["compile only fallback"]},
    )
    store.save_run(run)
    loaded = store.load_run("run_1")
    assert loaded.tool.name == "verilator"
    assert loaded.inputs["files"] == ["rtl/broken_counter.sv"]
    assert loaded.tool_result["validation_mode"] == "compile_only"
    assert loaded.tool_result["assumptions"] == ["compile only fallback"]


def test_run_store_list_skips_corrupt_run_records(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    run = VerificationRun(
        run_id="run_good",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="compile_repair",
        tool=ToolReference(kind="simulator", name="verilator"),
        inputs={"files": ["rtl/broken_counter.sv"]},
        status="passed",
        started_at="2026-04-07T00:00:00+00:00",
    )
    store.save_run(run)
    (store.runs_dir / "run_corrupt.json").write_text("{not json", encoding="utf-8")

    runs = store.list_runs()
    issues = store.list_run_load_issues()

    assert [item.run_id for item in runs] == ["run_good"]
    assert issues[0]["run_id"] == "run_corrupt"
    assert issues[0]["path"] == "runs/run_corrupt.json"
    assert "JSONDecodeError" in issues[0]["error"]


def test_run_store_redacts_sensitive_task_artifact_fields(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    path = store.save_task_artifact(
        "task_secret",
        "provider_request",
        {
            "headers": {"Authorization": "Bearer real-token", "X-Trace": "ok"},
            "env": {"TELCHINES_API_KEY": "real-token", "SAFE_VALUE": "ok"},
            "nested": {"password": "pw", "content": "keep"},
        },
    )
    payload = read_json(path)
    assert payload["headers"]["Authorization"] == "<redacted>"
    assert payload["headers"]["X-Trace"] == "ok"
    assert payload["env"]["TELCHINES_API_KEY"] == "<redacted>"
    assert payload["env"]["SAFE_VALUE"] == "ok"
    assert payload["nested"]["password"] == "<redacted>"
    assert payload["nested"]["content"] == "keep"


def test_import_manifest_persists_runs_and_triage_matches_history(sample_project: Path) -> None:
    imported_logs = sample_project / "logs" / "imported"
    imported_logs.mkdir(parents=True, exist_ok=True)
    failed_log = imported_logs / "seed_1.log"
    failed_log.write_text("rtl/uart_rx.sv:42: error: timeout waiting for start bit\n", encoding="utf-8")
    passed_log = imported_logs / "seed_2.log"
    passed_log.write_text("simulation passed\n", encoding="utf-8")
    manifest_path = sample_project / "regression_manifest.json"
    write_json(
        manifest_path,
        {
            "schema_version": "0.1",
            "tool": {"kind": "regression_manager", "name": "nightly", "version": "2026.06"},
            "runs": [
                {
                    "name": "uart_rx_seed_1",
                    "status": "failed",
                    "seed": 1,
                    "logs": ["logs/imported/seed_1.log"],
                    "waveforms": ["logs/regressions/uart_rx_trace.vcd"],
                    "artifacts": {"spec": "docs/uart.md"},
                    "metadata": {"suite": "smoke"},
                    "command": ["make", "regress", "SEED=1"],
                },
                {
                    "name": "uart_rx_seed_2",
                    "status": "passed",
                    "logs": ["logs/imported/seed_2.log"],
                },
            ],
        },
    )

    payload = import_runs(sample_project, Path("regression_manifest.json"))
    assert payload["imported_count"] == 2
    failed_run_id = payload["runs"][0]["run_id"]

    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    imported = [run for run in store.list_runs() if run.workflow_type == "regression_import"]
    assert len(imported) == 2
    failed = store.load_run(str(failed_run_id))
    assert failed.tool.name == "nightly"
    assert failed.observation_ids
    assert failed.artifacts["waveform_ids"]
    assert failed.artifacts["spec"] == "docs/uart.md"
    assert failed.replay_command == ["make", "regress", "SEED=1"]

    current_log_dir = sample_project / "logs" / "current"
    current_log_dir.mkdir(parents=True, exist_ok=True)
    current_log = current_log_dir / "seed_3.log"
    current_log.write_text("rtl/uart_rx.sv:42: error: timeout waiting for start bit\n", encoding="utf-8")
    triage_payload = triage(sample_project, [current_log])
    similar_run_ids = [match["run_id"] for match in triage_payload["clusters"][0]["similar_runs"]]
    assert failed_run_id in similar_run_ids


def test_import_manifest_dry_run_and_validation_errors(sample_project: Path) -> None:
    log_path = sample_project / "logs" / "imported" / "preview.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("rtl/demo.sv:7: syntax error\n", encoding="utf-8")
    manifest_path = sample_project / "preview_manifest.json"
    write_json(
        manifest_path,
        {
            "schema_version": "0.1",
            "tool": "fixture-regress",
            "runs": [{"name": "preview", "status": "failed", "logs": ["logs/imported/preview.log"]}],
        },
    )

    preview = import_runs(sample_project, manifest_path, dry_run=True)
    assert preview["dry_run"] is True
    assert preview["runs"][0]["stored"] is False
    assert RunStore(ProjectConfig.load(sample_project)).list_runs() == []

    missing = sample_project / "missing_manifest.json"
    try:
        import_runs(sample_project, missing)
    except ValueError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("expected missing manifest to fail")

    malformed = sample_project / "bad_manifest.json"
    malformed.write_text("{not json", encoding="utf-8")
    try:
        import_runs(sample_project, malformed)
    except ValueError as exc:
        assert "not valid JSON" in str(exc)
    else:
        raise AssertionError("expected malformed manifest to fail")

    write_json(manifest_path, {"schema_version": "9.9", "tool": "fixture", "runs": []})
    try:
        import_runs(sample_project, manifest_path)
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("expected unsupported schema to fail")

    write_json(
        manifest_path,
        {
            "schema_version": "0.1",
            "tool": "fixture",
            "runs": [{"name": "bad", "status": "failed", "logs": [str(log_path.resolve())]}],
        },
    )
    try:
        import_runs(sample_project, manifest_path)
    except ValueError as exc:
        assert "relative to the project root" in str(exc)
    else:
        raise AssertionError("expected absolute manifest path entry to fail")

    write_json(
        manifest_path,
        {
            "schema_version": "0.1",
            "tool": "fixture",
            "runs": [{"name": "bad", "status": "failed", "logs": ["../outside.log"]}],
        },
    )
    try:
        import_runs(sample_project, manifest_path)
    except ValueError as exc:
        assert "escapes the project root" in str(exc)
    else:
        raise AssertionError("expected escaping manifest path entry to fail")
