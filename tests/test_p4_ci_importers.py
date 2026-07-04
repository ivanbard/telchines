from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.ci_importers import github_actions_to_manifest, import_ci_runs, jenkins_to_manifest, junit_to_manifest
from telchines.config import ProjectConfig
from telchines.import_manifest import import_regression_payload
from telchines.run_store import RunStore


IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
STATUS = st.sampled_from(["passed", "failed", "error", "skipped", "unknown"])
CI_STATUS = st.sampled_from(["success", "failure", "failed", "error", "unstable", "cancelled", "skipped", "neutral", "in_progress"])
LOG_LINE = st.sampled_from(
    [
        "rtl/uart_rx.sv:42: error: timeout waiting for start bit",
        "UVM_ERROR tb/env.svh(7) @ 10ns: uvm_test_top.env [CFGERR] config_db lookup failed",
        "ERROR: [Synth 8-439] module uart_top not found [rtl/top.sv:17]",
        "",
    ]
)


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    status=STATUS,
    field_name=st.sampled_from(["log_text", "inline_log", "inline_logs"]),
    logs=st.lists(LOG_LINE, min_size=0, max_size=3),
    dry_run=st.booleans(),
)
def test_import_regression_payload_inline_logs_round_trip(sample_project: Path, status: str, field_name: str, logs: list[str], dry_run: bool) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    value: str | list[str]
    if field_name == "inline_logs":
        value = logs
    else:
        value = "\n".join(logs)
    item = {"name": "inline_seed", "status": status, field_name: value, "metadata": {"suite": "hypothesis"}}
    payload = {
        "schema_version": "0.1",
        "tool": {"kind": "regression_manager", "name": "inline", "version": "test"},
        "runs": [item],
    }

    imported = import_regression_payload(config, store, payload, manifest_label="inline.json", dry_run=dry_run)
    [run] = imported["runs"]

    non_empty_logs = [line for line in logs if line.strip()]
    expected_observations = sum(1 for line in non_empty_logs if any(token in line.lower() for token in ("error", "warning", "fatal", "uvm_", "synth")))
    assert imported["imported_count"] == 1
    assert run["status"] == status
    assert run["inline_log_count"] == (len(non_empty_logs) if field_name == "inline_logs" else (1 if "\n".join(logs).strip() else 0))
    assert run["observation_count"] >= min(expected_observations, run["inline_log_count"])
    assert run["stored"] is (not dry_run)


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    namespace=st.booleans(),
    failed=st.booleans(),
    error=st.booleans(),
    skipped=st.booleans(),
    system_out=LOG_LINE,
    system_err=LOG_LINE,
    class_name=IDENT,
    case_name=IDENT,
)
def test_junit_to_manifest_property_shapes(
    work_root: Path,
    namespace: bool,
    failed: bool,
    error: bool,
    skipped: bool,
    system_out: str,
    system_err: str,
    class_name: str,
    case_name: str,
) -> None:
    ns = "urn:junit" if namespace else ""
    tag = (lambda value: f"{{{ns}}}{value}") if namespace else (lambda value: value)
    suite = ET.Element(tag("testsuite"), {"name": "nightly"})
    case = ET.SubElement(suite, tag("testcase"), {"classname": class_name, "name": case_name, "time": "1.0"})
    if failed:
        ET.SubElement(case, tag("failure")).text = "rtl/uart_rx.sv:42: error: timeout waiting for start bit"
    if error:
        ET.SubElement(case, tag("error")).text = "rtl/uart_rx.sv:43: error: unknown identifier tx_fifo_level"
    if skipped:
        ET.SubElement(case, tag("skipped"))
    ET.SubElement(case, tag("system-out")).text = system_out
    ET.SubElement(case, tag("system-err")).text = system_err
    source = work_root / "junit.xml"
    ET.ElementTree(suite).write(source, encoding="utf-8", xml_declaration=True)

    manifest = junit_to_manifest(source)
    [run] = manifest["runs"]

    assert manifest["schema_version"] == "0.1"
    assert manifest["tool"]["name"] == "junit"
    assert run["name"] == f"{class_name}.{case_name}"
    assert run["status"] == ("failed" if failed or error else "skipped" if skipped else "passed")
    assert run["metadata"]["suite"] == "nightly"


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    as_wrapped_jobs=st.booleans(),
    conclusion=CI_STATUS,
    run_id=st.one_of(st.just(""), st.integers(min_value=1, max_value=999999).map(str)),
    job_name=IDENT,
    step_failed=st.booleans(),
    annotation_message=st.one_of(st.just(""), st.sampled_from(["timeout waiting for start bit", "unknown identifier tx_fifo_level"])),
)
def test_github_actions_to_manifest_property_shapes(
    work_root: Path,
    as_wrapped_jobs: bool,
    conclusion: str,
    run_id: str,
    job_name: str,
    step_failed: bool,
    annotation_message: str,
) -> None:
    job = {
        "id": 7,
        "name": job_name,
        "conclusion": conclusion,
        "steps": [{"name": "sim", "conclusion": "failure" if step_failed else "success"}],
        "annotations": [{"path": "rtl/top.sv", "line": 11, "message": annotation_message}] if annotation_message else [],
    }
    payload = {"workflow_name": "nightly", "run_id": run_id, "jobs": {"jobs": [job]} if as_wrapped_jobs else [job]}
    source = work_root / "gha.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    manifest = github_actions_to_manifest(source)
    [run] = manifest["runs"]

    assert manifest["tool"]["name"] == "github-actions"
    assert run["name"] == job_name
    assert run["metadata"]["workflow"] == "nightly"
    assert run["metadata"]["run_id"] == run_id
    assert run["command"] == (["gh", "run", "rerun", run_id] if run_id else [])
    if annotation_message:
        assert annotation_message in run["log_text"]


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    multi_build=st.booleans(),
    result=CI_STATUS,
    case_status=st.sampled_from(["PASSED", "FAILED", "REGRESSION", ""]),
    has_cases=st.booleans(),
    build_name=IDENT,
)
def test_jenkins_to_manifest_property_shapes(work_root: Path, multi_build: bool, result: str, case_status: str, has_cases: bool, build_name: str) -> None:
    build = {"fullDisplayName": build_name, "result": result, "url": "https://ci.example/job/1"}
    if has_cases:
        build["testResult"] = {
            "suites": [
                {
                    "cases": [
                        {
                            "className": "uart",
                            "name": "seed_1",
                            "status": case_status,
                            "errorDetails": "rtl/uart_rx.sv:42: error: timeout waiting for start bit" if case_status in {"FAILED", "REGRESSION"} else "",
                        }
                    ]
                }
            ]
        }
    else:
        build["log"] = "rtl/uart_rx.sv:42: error: timeout waiting for start bit"
    payload = {"builds": [build]} if multi_build else build
    source = work_root / "jenkins.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    manifest = jenkins_to_manifest(source)
    [run] = manifest["runs"]

    assert manifest["tool"]["name"] == "jenkins"
    assert run["metadata"]["source_format"] == "jenkins"
    if has_cases:
        assert run["name"] == "seed_1"
    else:
        assert run["name"] == build_name
        assert run["log_text"]


def test_ci_importer_invalid_inputs_report_clear_errors(sample_project: Path, work_root: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    missing = work_root / "missing.json"
    existing = work_root / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    malformed = work_root / "bad.json"
    malformed.write_text("[1, 2, 3]", encoding="utf-8")
    bad_xml = work_root / "bad.xml"
    bad_xml.write_text("<testsuite>", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported CI importer"):
        import_ci_runs(config, store, existing, importer="unknown")
    with pytest.raises(ValueError, match="import source does not exist"):
        import_ci_runs(config, store, missing, importer="junit")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        github_actions_to_manifest(malformed)
    with pytest.raises(ET.ParseError):
        junit_to_manifest(bad_xml)
    with pytest.raises(ValueError, match="import manifest run 1 metadata must be an object"):
        import_regression_payload(
            config,
            store,
            {"schema_version": "0.1", "tool": "bad", "runs": [{"name": "bad", "status": "failed", "metadata": []}]},
            manifest_label="bad.json",
            dry_run=True,
        )
