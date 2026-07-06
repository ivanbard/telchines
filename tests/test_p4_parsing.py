from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.adapters.parsing import normalize_signature, parse_common_output
from telchines.config import ProjectConfig
from telchines.models import FailureCluster, Observation
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.workflows.triage import triage_logs


IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
PATH_PART = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}", fullmatch=True)
SV_PATH = st.builds(lambda directory, name: f"{directory}/{name}.svh", PATH_PART, PATH_PART)
RTL_PATH = st.builds(lambda name: f"rtl/{name}.sv", PATH_PART)
COMPONENT = st.lists(IDENT, min_size=2, max_size=5).map(lambda parts: ".".join(parts))
TIME_TEXT = st.builds(lambda value, unit: f"{value}{unit}", st.integers(min_value=0, max_value=999999), st.sampled_from(["ns", "ps", "us"]))
LINE_NUMBER = st.integers(min_value=1, max_value=5000)

UVM_CASES = st.sampled_from(
    [
        ("PHASE_TIMEOUT", "run phase objection timeout waiting for drain", "UVM_PHASE_OBJECTION_TIMEOUT"),
        ("CFGERR", "config_db lookup failed for virtual interface", "UVM_CONFIG_DB_ERROR"),
        ("VIF", "virtual interface vif was not supplied", "UVM_VIRTUAL_INTERFACE_ERROR"),
        ("SB_MISMATCH", "scoreboard mismatch expected observed transaction", "UVM_SCOREBOARD_MISMATCH"),
        ("SEQ_TIMEOUT", "sequence timed out waiting for response", "UVM_SEQUENCE_TIMEOUT"),
    ]
)

VENDOR_CASES = st.sampled_from(
    [
        ("vivado", "ERROR", "Synth 8-439", "module uart_top not found", "VIVADO_SYNTH_ERROR"),
        ("vivado", "CRITICAL WARNING", "Timing 38-282", "timing constraint was not met", "VIVADO_TIMING_ERROR"),
        ("quartus", "Error", "14566", "The Fitter cannot place component", "QUARTUS_FITTER_ERROR"),
        ("quartus", "Warning", "332012", "TimeQuest timing requirement is not met", "QUARTUS_TIMING_ERROR"),
        ("libero", "ERROR", "TIMING-003", "Timing constraint violation", "LIBERO_TIMING_ERROR"),
        ("libero", "Warning", "SYN-101", "synthesis inferred latch", "LIBERO_SYNTH_ERROR"),
    ]
)


@settings(max_examples=60)
@given(severity=st.sampled_from(["UVM_ERROR", "UVM_FATAL", "UVM_WARNING"]), path=SV_PATH, line=LINE_NUMBER, time=TIME_TEXT, component=COMPONENT, case=UVM_CASES)
def test_generated_uvm_lines_preserve_metadata(
    severity: str,
    path: str,
    line: int,
    time: str,
    component: str,
    case: tuple[str, str, str],
) -> None:
    code, message, expected_signature = case
    text = f"{severity} {path}({line}) @ {time}: {component} [{code}] {message}"

    [observation] = parse_common_output("run_uvm", text)

    assert observation.signature == expected_signature
    assert observation.file == str(Path(path))
    assert observation.line == line
    assert observation.log_family == "uvm"
    assert observation.tool_name == "uvm"
    assert observation.metadata["code"] == code
    assert observation.metadata["component"] == component
    assert observation.metadata["time"] == time
    assert observation.severity in {"error", "warning"}


@settings(max_examples=60)
@given(case=VENDOR_CASES, path=RTL_PATH, line=LINE_NUMBER)
def test_generated_vendor_lines_preserve_metadata(case: tuple[str, str, str, str, str], path: str, line: int) -> None:
    tool, severity, code, message, expected_signature = case
    if tool == "vivado":
        text = f"{severity}: [{code}] {message} [{path}:{line}]"
    elif tool == "quartus":
        text = f"{severity} ({code}): {message} at {path}({line})"
    else:
        text = f"{severity}: {code}: {message} File: {path}, Line: {line}"

    [observation] = parse_common_output("run_vendor", text)

    assert observation.signature == expected_signature
    assert observation.file == str(Path(path))
    assert observation.line == line
    assert observation.log_family == "vendor_build"
    assert observation.tool_name == tool
    assert observation.metadata["code"] == code
    assert observation.severity in {"error", "warning"}


def test_generic_parser_fallback_remains_backward_compatible() -> None:
    observations = parse_common_output("run_1", "ERROR: rtl/demo.sv:12: expected semicolon before end")

    assert len(observations) == 1
    assert observations[0].signature == "SV_PARSE_EXPECTED_SEMICOLON"
    assert observations[0].log_family == ""
    assert observations[0].tool_name == ""
    assert observations[0].metadata == {"raw_line": "ERROR: rtl/demo.sv:12: expected semicolon before end"}
    assert normalize_signature("unknown identifier tx_fifo_level") == "SV_UNKNOWN_IDENTIFIER"
    assert normalize_signature("expected endmodule at end of file") == "SV_EXPECTED_ENDMODULE"
    assert normalize_signature("timeout waiting for start bit") == "SIM_TIMEOUT"


@settings(max_examples=75)
@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
def test_arbitrary_output_never_crashes_parser(text: str) -> None:
    observations = parse_common_output("run_noise", text)

    for observation in observations:
        assert observation.observation_id.startswith("obs_")
        assert observation.run_id == "run_noise"
        assert observation.signature
        assert observation.severity in {"error", "warning", "info"}


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(case=st.sampled_from(["uvm", "vendor", "generic", "imported"]))
def test_triage_selects_retrieval_mode_by_log_family(work_root: Path, case: str) -> None:
    project_root = work_root / f"triage_{case}"
    config = ProjectConfig.init_project(project_root)
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if case == "uvm":
        expected_mode = "uvm_triage"
        log_text = "UVM_ERROR tb/env.svh(7) @ 10ns: uvm_test_top.env [PHASE_TIMEOUT] run phase objection timeout"
    elif case == "vendor":
        expected_mode = "vendor_build"
        log_text = "ERROR: [Synth 8-439] module uart_top not found [rtl/top.sv:17]"
    elif case == "imported":
        expected_mode = "regression"
        log_text = "rtl/top.sv:17: error: timeout waiting for start bit"
    else:
        expected_mode = "triage"
        log_text = "rtl/top.sv:17: error: timeout waiting for start bit"
    (logs_dir / "run.log").write_text(log_text, encoding="utf-8")

    store = RunStore(config)
    retrieval = RetrievalService(config)
    run, clusters, context = triage_logs(config, store, retrieval, logs_dir)
    if case == "imported":
        # The imported mode is only selected when observations originate from import manifests.
        assert run.inputs["retrieval_mode"] == "triage"
    else:
        assert run.inputs["retrieval_mode"] == expected_mode
        assert context.mode == expected_mode
    assert clusters


def test_old_observation_and_cluster_payloads_load_with_new_defaults(sample_project: Path) -> None:
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    old_payload = {
        "observation_id": "obs_old",
        "run_id": "run_old",
        "type": "tool_error",
        "signature": "SIM_TIMEOUT",
        "file": None,
        "line": None,
        "message": "timeout",
        "severity": "error",
    }
    (store.observations_dir / "obs_old.json").write_text(__import__("json").dumps(old_payload), encoding="utf-8")

    loaded = store.load_observation("obs_old")
    cluster = FailureCluster(cluster_id="cluster_old", signature="SIM_TIMEOUT", count=1, files=[], summary="old", observation_ids=[])

    assert loaded.log_family == ""
    assert loaded.tool_name == ""
    assert loaded.metadata == {}
    assert cluster.log_family == ""
    assert cluster.tool_name == ""
    assert cluster.domain == ""
