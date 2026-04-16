from __future__ import annotations

from pathlib import Path

import pytest

from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
from telchines.retrieval import RetrievalService
from telchines.run_store import RunStore
from telchines.utils import write_json
from telchines.workflows.coverage import execute_coverage_plan, load_coverage_report


def test_coverage_plan_classifies_stimulus_and_checker_gaps(sample_project: Path) -> None:
    coverage_path = sample_project / "cov" / "coverage.json"
    write_json(
        coverage_path,
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
                    "goal": 3,
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
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()

    plan, run, context = execute_coverage_plan(config, store, retrieval, coverage_path)

    assert run.workflow_type == "coverage_plan"
    assert context.mode == "coverage"
    assert len(plan.recommendations) == 2
    assert plan.recommendations[0].classification == "missing_stimulus"
    assert plan.recommendations[1].classification == "missing_checker"
    assert plan.recommendations[0].evidence_citations
    assert "planned 2 coverage actions" in run.summary


def test_coverage_plan_uses_formal_run_for_unreachable_classification(sample_project: Path) -> None:
    coverage_path = sample_project / "cov" / "unreachable.json"
    write_json(
        coverage_path,
        {
            "tool": "fixture_cov",
            "generated_at": "2026-04-15T00:00:00+00:00",
            "design": "uart_rx",
            "items": [
                {
                    "item_id": "rx_dead_state",
                    "module": "uart_rx",
                    "metric": "fsm",
                    "name": "unreachable_rx_state",
                    "hits": 0,
                    "goal": 1,
                    "detail": "Dead state bin remains uncovered.",
                }
            ],
        },
    )
    config = ProjectConfig.load(sample_project)
    store = RunStore(config)
    retrieval = RetrievalService(config)
    retrieval.build_index()
    formal_run = VerificationRun(
        run_id="formal_unreachable_1",
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="formal_validation",
        tool=ToolReference(kind="formal", name="symbiyosys"),
        inputs={},
        status="passed",
        started_at="2026-04-15T00:00:00+00:00",
        finished_at="2026-04-15T00:01:00+00:00",
        exit_code=0,
        tool_result={
            "status": "passed",
            "property_ids": ["unreachable_rx_state"],
            "report_paths": ["formal/unreachable_rx_state.txt"],
        },
        summary="Formal marked unreachable_rx_state unreachable under current constraints.",
    )
    store.save_run(formal_run)

    plan, _, _ = execute_coverage_plan(config, store, retrieval, coverage_path, formal_run_id=formal_run.run_id)

    assert plan.recommendations[0].classification == "dead_or_unreachable"
    assert plan.recommendations[0].supporting_run_ids == [formal_run.run_id]
    assert "exclusion candidate" in plan.recommendations[0].suggested_action.lower()


def test_load_coverage_report_rejects_invalid_schema(sample_project: Path) -> None:
    coverage_path = sample_project / "cov" / "invalid.json"
    write_json(
        coverage_path,
        {
            "tool": "fixture_cov",
            "generated_at": "2026-04-15T00:00:00+00:00",
            "design": "uart_rx",
            "items": [{"module": "uart_rx"}],
        },
    )
    with pytest.raises(ValueError, match="item_id"):
        load_coverage_report(coverage_path)
