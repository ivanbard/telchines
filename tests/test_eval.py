from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.config import ProjectConfig
from telchines.errors import ConfigError
from telchines.eval import _aggregate_metrics, _benchmark_status, _cocotb_executable_expectation_met, run_default_suite
from telchines.operations import load_eval_report, run_eval
from telchines.run_store import RunStore
from telchines.setup import UserSetup, default_model_policy


DIR_NAME = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{0,12}", fullmatch=True)


def _tiny_eval_report(*_args, **_kwargs) -> dict[str, object]:
    return {
        "suite": "default",
        "ran_at": "2026-07-04T00:00:00+00:00",
        "cases": [],
        "passed": 1,
        "total": 1,
        "metrics": {},
    }


def test_cocotb_benchmark_executable_expectation_tracks_environment_contract() -> None:
    assert _cocotb_executable_expectation_met("when_supported", "supported", "passed")
    assert _cocotb_executable_expectation_met("when_supported", "unsupported", "skipped")
    assert not _cocotb_executable_expectation_met("when_supported", "supported", "skipped")
    assert not _cocotb_executable_expectation_met("when_supported", "unsupported", "failed")


def test_eval_default_suite(work_root: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    project_root = work_root / "eval_project"
    project_root.mkdir()
    shutil.copytree(repo_root / "benchmarks", project_root / "benchmarks")
    ProjectConfig.init_project(project_root)
    config = ProjectConfig.load(project_root)
    store = RunStore(config)
    report = run_default_suite(config, store)
    assert report["passed"] == report["total"]
    assert report["total"] == 30
    assert report["metrics"]["agent"]["cases"] == 1
    assert report["metrics"]["agent"]["pass_rate"] == 1.0
    assert report["metrics"]["agent"]["review_required_count"] == 1
    assert report["metrics"]["retrieval"]["cases"] == 4
    assert report["metrics"]["retrieval"]["avg_recall_at_k"] >= 0.95
    assert report["metrics"]["retrieval"]["avg_external_recall_at_k"] == 1.0
    assert report["metrics"]["sva"]["cases"] == 3
    assert report["metrics"]["sva"]["generation_rate"] == 1.0
    assert report["metrics"]["sva"]["validation_pass_rate"] == 0.667
    assert report["metrics"]["sva"]["artifact_generation_rate"] == 1.0
    assert report["metrics"]["sva"]["avg_property_count"] == 1.0
    assert report["metrics"]["sva"]["avg_property_name_match_rate"] == 1.0
    assert report["metrics"]["sva"]["avg_citation_match_rate"] == 1.0
    assert report["metrics"]["sva"]["validation_modes"]
    assert sum(report["metrics"]["sva"]["validation_modes"].values()) == report["metrics"]["sva"]["cases"]
    assert sum(report["metrics"]["sva"]["formal_statuses"].values()) == report["metrics"]["sva"]["cases"]
    assert report["metrics"]["cocotb"]["cases"] == 3
    assert report["metrics"]["cocotb"]["generation_rate"] == 1.0
    assert report["metrics"]["cocotb"]["validation_pass_rate"] == 1.0
    assert report["metrics"]["cocotb"]["manifest_generation_rate"] == 1.0
    assert sum(report["metrics"]["cocotb"]["validation_modes"].values()) == report["metrics"]["cocotb"]["cases"]
    assert sum(report["metrics"]["cocotb"]["executable_statuses"].values()) == report["metrics"]["cocotb"]["cases"]
    cocotb_cases = [case for case in report["cases"] if case["task_type"] == "cocotb"]
    assert all(case["executable_expectation"] == "when_supported" for case in cocotb_cases)
    assert all(
        (case["executable_contract"] == "supported" and case["executable_status"] == "passed")
        or (case["executable_contract"] == "unsupported" and case["executable_status"] == "skipped")
        for case in cocotb_cases
    )
    assert report["metrics"]["coverage"]["cases"] == 3
    assert report["metrics"]["coverage"]["avg_recommendation_count"] >= 1.0
    assert report["metrics"]["coverage"]["avg_evidence_count"] >= 1.0
    assert report["metrics"]["triage"]["cases"] == 4
    assert report["metrics"]["import"]["cases"] == 3
    assert report["metrics"]["import"]["avg_imported_count"] == 2.0
    assert report["metrics"]["coverage_import"]["cases"] == 2
    assert report["metrics"]["coverage_import"]["avg_item_count"] == 2.0
    assert report["metrics"]["provider_response"]["cases"] == 2
    assert report["metrics"]["provider_response"]["provider_error_count"] == 2
    assert report["metrics"]["provider_response"]["generated_candidate_count"] == 0
    assert report["metrics"]["readiness"]["case_scope_counts"]["fixture_large_design"] == 1
    assert report["metrics"]["readiness"]["execution_backing_counts"]["fixture_provider_response"] == 2
    assert (
        report["metrics"]["readiness"]["structure_or_fixture_only_case_count"]
        + report["metrics"]["readiness"]["tool_backed_case_count"]
        == report["total"]
    )
    assert "project_context" not in report


def test_eval_surfaces_optional_formal_failures_as_warnings() -> None:
    result = {
        "task_type": "sva",
        "passed": True,
        "benchmark_status": _benchmark_status(True, "passed_with_warnings"),
        "generated_candidate": True,
        "validation_status": "passed",
        "validation_mode": "structure_only",
        "structural_status": "passed",
        "syntax_status": "not_run",
        "adapter_status": "not_run",
        "formal_status": "failed",
        "proof_status": "not_proved",
        "overall_status": "passed_with_warnings",
        "artifact_generated": True,
        "property_count": 1,
        "property_name_match_rate": 1.0,
        "citation_match_rate": 1.0,
        "benchmark_scope": "fixture",
        "execution_backing": "formal_tool",
    }

    metrics = _aggregate_metrics([result])

    assert result["benchmark_status"] == "passed_with_warnings"
    assert metrics["sva"]["formal_failure_count"] == 1
    assert metrics["sva"]["overall_statuses"] == {"passed_with_warnings": 1}
    assert metrics["readiness"]["benchmark_status_counts"] == {"passed_with_warnings": 1}


def test_eval_default_suite_uses_bundled_benchmarks(work_root: Path) -> None:
    project_root = work_root / "installed_style_project"
    project_root.mkdir()
    ProjectConfig.init_project(project_root)
    config = ProjectConfig.load(project_root)
    store = RunStore(config)

    report = run_default_suite(config, store)

    assert report["passed"] == report["total"]
    assert report["total"] == 30
    assert store.load_report("latest_eval")["total"] == 30


def test_eval_uses_isolated_fixture_commands_without_relaxing_project_policy(work_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELCHINES_CONFIG_DIR", str(work_root / "settings"))
    UserSetup(
        completed=True,
        model_mode="local",
        no_egress=True,
        allow_local_commands=False,
        artifact_storage_acknowledged=True,
        model_policy=default_model_policy(),
    ).save()
    project_root = work_root / "offline_eval_project"
    project_root.mkdir()
    config = ProjectConfig.init_project(project_root)
    assert config.allow_local_commands is False

    report = run_default_suite(config, RunStore(config))

    assert report["passed"] == report["total"] == 30
    assert report["evaluation_environment"] == {
        "type": "disposable_benchmark_project",
        "fixture_local_commands_enabled": True,
        "policy_scope": "copied benchmark fixtures only",
    }
    assert ProjectConfig.load(project_root).allow_local_commands is False
    assert RunStore(ProjectConfig.load(project_root)).load_report("latest_eval")["evaluation_environment"] == report["evaluation_environment"]


def test_eval_operation_uses_project_context_and_persists_report(work_root: Path) -> None:
    project_root = work_root / "project_eval_context"
    project_root.mkdir()
    ProjectConfig.init_project(project_root)

    report = run_eval(project_root)

    assert report["passed"] == report["total"]
    assert report["project_context"] == "project"
    assert report["report_persisted"] is True
    config = ProjectConfig.load(project_root)
    assert RunStore(config).load_report("latest_eval")["total"] == report["total"]


def test_eval_operation_uses_non_mutating_scratch_context_with_local_benchmarks(work_root: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source_like_root = work_root / "source_like_no_project"
    source_like_root.mkdir()
    shutil.copytree(repo_root / "benchmarks", source_like_root / "benchmarks")

    report = run_eval(source_like_root)

    assert report["passed"] == report["total"]
    assert report["project_context"] == "scratch"
    assert report["report_persisted"] is False
    assert report["benchmark_source"] == str(source_like_root / "benchmarks")
    assert not (source_like_root / ".tel").exists()
    assert not Path(str(report["scratch_project"])).exists()


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dirname=DIR_NAME)
def test_eval_scratch_property_is_non_mutating_and_non_persisted(work_root: Path, monkeypatch, dirname: str) -> None:
    monkeypatch.setattr("telchines.operations.run_default_suite", _tiny_eval_report)
    root = work_root / dirname
    root.mkdir(exist_ok=True)

    report = run_eval(root)

    assert report["project_context"] == "scratch"
    assert report["report_persisted"] is False
    assert report["passed"] == report["total"] == 1
    assert not (root / ".tel").exists()
    assert not Path(str(report["scratch_project"])).exists()


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dirname=DIR_NAME)
def test_eval_project_property_persists_report_context(work_root: Path, monkeypatch, dirname: str) -> None:
    def fake_run_default_suite(_config: ProjectConfig, store: RunStore) -> dict[str, object]:
        report = _tiny_eval_report()
        store.save_report("latest_eval", report)
        return report

    monkeypatch.setattr("telchines.operations.run_default_suite", fake_run_default_suite)
    root = work_root / f"project_{dirname}"
    root.mkdir(exist_ok=True)
    ProjectConfig.init_project(root)

    report = run_eval(root)

    assert report["project_context"] == "project"
    assert report["report_persisted"] is True
    assert (root / ".tel").is_dir()
    assert load_eval_report(root)["total"] == 1


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dirname=DIR_NAME)
def test_eval_report_outside_project_has_clear_error(work_root: Path, dirname: str) -> None:
    root = work_root / f"no_report_{dirname}"
    root.mkdir(exist_ok=True)

    with pytest.raises(ConfigError, match="persisted only for initialized Telchines projects"):
        load_eval_report(root)
