from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from telchines.models import ToolReference, VerificationRun
from telchines.operations import _repair_review_status as operations_repair_review_status
from telchines.operations import _repair_workflow_status as operations_repair_workflow_status
from telchines.operations import _validation_mode as operations_validation_mode
from telchines.workflows.agent import _repair_review_status as agent_repair_review_status
from telchines.workflows.agent import _repair_workflow_status as agent_repair_workflow_status
from telchines.workflows.agent import _validation_mode as agent_validation_mode


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "provider_capability_study.py"
SPEC = importlib.util.spec_from_file_location("provider_capability_study", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
provider_capability_study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider_capability_study)


STATUS_TEXT = st.text(min_size=0, max_size=24)
KNOWN_CANDIDATE_STATUSES = st.sampled_from(["proposed", "validated", "rejected", "no_patch", "no_generation", ""])
KNOWN_VALIDATION_STATUSES = st.one_of(st.none(), st.sampled_from(["passed", "failed", "skipped", ""]))
WORKFLOW_STATUSES = st.sampled_from(["review_required", "applied", "validated", "rejected", "failed", "no_patch", "no_generation", "planned", "triaged", ""])


@settings(max_examples=50)
@given(candidate_status=KNOWN_CANDIDATE_STATUSES, validation_status=KNOWN_VALIDATION_STATUSES, apply_patch=st.booleans())
def test_repair_workflow_status_lattice(candidate_status: str, validation_status: str | None, apply_patch: bool) -> None:
    for helper in (operations_repair_workflow_status, agent_repair_workflow_status):
        result = helper(candidate_status, validation_status, apply_patch=apply_patch)
        if candidate_status == "no_patch":
            assert result == "no_patch"
        elif validation_status == "passed":
            assert result == ("applied" if apply_patch else "review_required")
        elif validation_status == "failed" or candidate_status == "rejected":
            assert result == "rejected"


@settings(max_examples=50)
@given(workflow_status=WORKFLOW_STATUSES)
def test_repair_review_status_maps_only_review_and_apply_states(workflow_status: str) -> None:
    expected = "pending_review" if workflow_status == "review_required" else "applied" if workflow_status == "applied" else "not_available"
    assert operations_repair_review_status(workflow_status) == expected
    assert agent_repair_review_status(workflow_status) == expected


@settings(max_examples=50)
@given(mode=STATUS_TEXT)
def test_validation_mode_extracts_nonempty_tool_result_mode(mode: str) -> None:
    run = VerificationRun(
        run_id="run_test",
        project_id="proj_test",
        commit_sha="workspace",
        workflow_type="repair_validation",
        tool=ToolReference(kind="validator", name="fixture"),
        inputs={},
        status="passed",
        started_at="2026-01-01T00:00:00+00:00",
        tool_result={"validation_mode": mode},
    )
    expected = mode.strip() or None
    assert operations_validation_mode(run) == expected
    assert agent_validation_mode(run) == expected
    assert operations_validation_mode(None) is None
    assert agent_validation_mode(None) is None


@settings(max_examples=50)
@given(label=STATUS_TEXT, returncode=st.integers(min_value=1, max_value=255), parsed=st.dictionaries(st.text(max_size=10), st.integers(), max_size=5))
def test_provider_scorer_nonzero_exit_always_fails(label: str, returncode: int, parsed: dict[str, int]) -> None:
    status, reason = provider_capability_study._score_command_result({"label": label}, returncode, parsed)
    assert status == "failed"
    assert reason == f"process_exit:{returncode}"


@settings(max_examples=50)
@given(parsed_status=STATUS_TEXT)
def test_provider_check_passes_only_when_payload_status_passed(parsed_status: str) -> None:
    status, _ = provider_capability_study._score_command_result({"label": "provider_check"}, 0, {"status": parsed_status})
    assert status == ("passed" if parsed_status == "passed" else "failed")


@settings(max_examples=50)
@given(
    workflow_status=STATUS_TEXT,
    validation_status=st.one_of(st.none(), STATUS_TEXT),
    patch_location=st.sampled_from(["top", "result", "evidence", "missing"]),
)
def test_agent_repair_scorer_requires_validated_patch(workflow_status: str, validation_status: str | None, patch_location: str) -> None:
    parsed: dict[str, object] = {"status": workflow_status, "validation_status": validation_status}
    if patch_location == "top":
        parsed["patch_id"] = "patch_top"
    elif patch_location == "result":
        parsed["result"] = {"patch_id": "patch_result"}
    elif patch_location == "evidence":
        parsed["evidence"] = {"patch_id": "patch_evidence"}

    status, _ = provider_capability_study._score_command_result({"label": "agent_repair"}, 0, parsed)

    expected_pass = workflow_status in {"review_required", "applied"} and validation_status == "passed" and patch_location != "missing"
    assert status == ("passed" if expected_pass else "failed")


@settings(max_examples=50)
@given(
    label=st.sampled_from(["gen_sva", "gen_cocotb"]),
    validation_status=st.one_of(st.none(), STATUS_TEXT),
    has_candidate=st.booleans(),
    expected_no_generation=st.booleans(),
)
def test_generation_scorer_requires_validated_candidate_or_expected_no_generation(
    label: str,
    validation_status: str | None,
    has_candidate: bool,
    expected_no_generation: bool,
) -> None:
    parsed: dict[str, object] = {
        "status": "no_generation" if expected_no_generation else "validated",
        "validation_status": validation_status,
    }
    if has_candidate:
        parsed["candidate_id"] = "candidate_1"
    command: dict[str, object] = {"label": label}
    if expected_no_generation:
        command["expected"] = "no_generation"

    status, _ = provider_capability_study._score_command_result(command, 0, parsed)

    expected_pass = expected_no_generation or (has_candidate and validation_status == "passed")
    assert status == ("passed" if expected_pass else "failed")


@settings(max_examples=50)
@given(
    top_validation=st.one_of(st.none(), STATUS_TEXT),
    nested_validation=st.one_of(st.none(), STATUS_TEXT),
    top_candidate=st.one_of(st.none(), STATUS_TEXT),
    nested_candidate=st.one_of(st.none(), STATUS_TEXT),
    top_patch=st.one_of(st.none(), STATUS_TEXT),
    result_patch=st.one_of(st.none(), STATUS_TEXT),
    evidence_patch=st.one_of(st.none(), STATUS_TEXT),
    result_is_dict=st.booleans(),
    evidence_is_dict=st.booleans(),
)
def test_payload_extractors_prefer_top_level_and_survive_malformed_nested_values(
    top_validation: str | None,
    nested_validation: str | None,
    top_candidate: str | None,
    nested_candidate: str | None,
    top_patch: str | None,
    result_patch: str | None,
    evidence_patch: str | None,
    result_is_dict: bool,
    evidence_is_dict: bool,
) -> None:
    parsed: dict[str, object] = {}
    if top_validation is not None:
        parsed["validation_status"] = top_validation
    if top_candidate is not None:
        parsed["candidate_id"] = top_candidate
    if top_patch is not None:
        parsed["patch_id"] = top_patch
    parsed["result"] = {"validation_status": nested_validation, "candidate_id": nested_candidate, "patch_id": result_patch} if result_is_dict else "not-a-dict"
    parsed["evidence"] = {"patch_id": evidence_patch} if evidence_is_dict else ["not-a-dict"]

    expected_validation = top_validation if top_validation is not None else nested_validation if result_is_dict else None
    expected_candidate = top_candidate or (nested_candidate if result_is_dict else None)
    expected_patch = top_patch or (result_patch if result_is_dict and result_patch else None) or (evidence_patch if evidence_is_dict else None)

    assert provider_capability_study._payload_validation_status(parsed) == expected_validation
    assert provider_capability_study._payload_candidate_id(parsed) == expected_candidate
    assert provider_capability_study._payload_patch_id(parsed) == expected_patch


PROVIDER_NAME = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"), min_size=1, max_size=12).filter(lambda value: value != "__inactive_provider__")
SAFE_TEXT = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_."), min_size=1, max_size=12)


@settings(max_examples=50)
@given(planned=st.lists(PROVIDER_NAME, min_size=0, max_size=8, unique=True))
def test_active_providers_are_exactly_providers_with_planned_commands(planned: list[str]) -> None:
    providers = [{"name": name, "kind": "heuristic"} for name in planned + ["__inactive_provider__"]]
    commands = [{"provider": name, "status": "planned", "command": []} for name in planned]
    commands.append({"provider": "__inactive_provider__", "status": "skipped", "command": []})

    active = provider_capability_study._active_providers(providers, commands)

    assert [provider["name"] for provider in active] == planned


@settings(max_examples=50)
@given(provider=PROVIDER_NAME, label=SAFE_TEXT, model_a=SAFE_TEXT, model_b=SAFE_TEXT, reasoning_a=SAFE_TEXT, reasoning_b=SAFE_TEXT)
def test_stability_metrics_group_by_provider_label_model_and_reasoning(
    provider: str,
    label: str,
    model_a: str,
    model_b: str,
    reasoning_a: str,
    reasoning_b: str,
) -> None:
    assume((model_a, reasoning_a) != (model_b, reasoning_b))
    results = [
        {
            "provider": provider,
            "label": label,
            "model": model_a,
            "reasoning_level": reasoning_a,
            "status": "passed",
            "semantic_fingerprint": "fp-a",
            "elapsed_seconds": 0.1,
        },
        {
            "provider": provider,
            "label": label,
            "model": model_b,
            "reasoning_level": reasoning_b,
            "status": "passed",
            "semantic_fingerprint": "fp-b",
            "elapsed_seconds": 0.2,
        },
    ]

    metrics = provider_capability_study._stability_metrics(results)

    keys = {(item["provider"], item["label"], item["model"], item["reasoning_level"]) for item in metrics}
    assert keys == {(provider, label, model_a, reasoning_a), (provider, label, model_b, reasoning_b)}
    assert all(item["runs"] == 1 for item in metrics)


@settings(max_examples=50)
@given(status_a=SAFE_TEXT, status_b=SAFE_TEXT, fingerprint_a=SAFE_TEXT, fingerprint_b=SAFE_TEXT)
def test_stability_metrics_require_single_status_and_fingerprint(
    status_a: str,
    status_b: str,
    fingerprint_a: str,
    fingerprint_b: str,
) -> None:
    results = [
        {
            "provider": "provider",
            "label": "scenario",
            "model": "model",
            "reasoning_level": "high",
            "status": status_a,
            "semantic_fingerprint": fingerprint_a,
            "elapsed_seconds": 0.1,
        },
        {
            "provider": "provider",
            "label": "scenario",
            "model": "model",
            "reasoning_level": "high",
            "status": status_b,
            "semantic_fingerprint": fingerprint_b,
            "elapsed_seconds": 0.3,
        },
    ]

    [metric] = provider_capability_study._stability_metrics(results)

    assert metric["runs"] == 2
    assert metric["stable"] is (status_a == status_b and fingerprint_a == fingerprint_b)
    assert metric["latency_seconds_min"] == 0.1
    assert metric["latency_seconds_max"] == 0.3


@settings(max_examples=50)
@given(status=SAFE_TEXT, validation=st.one_of(st.none(), SAFE_TEXT), candidate=st.one_of(st.none(), SAFE_TEXT), patch=st.one_of(st.none(), SAFE_TEXT))
def test_semantic_fingerprint_is_deterministic_bounded_and_secret_free(
    status: str,
    validation: str | None,
    candidate: str | None,
    patch: str | None,
) -> None:
    parsed = {
        "status": status,
        "validation_status": validation,
        "candidate_id": candidate,
        "patch_id": patch,
        "stdout": "secret-token",
        "stderr": "Authorization: secret-token",
    }
    command = {"label": "gen_sva"}

    first = provider_capability_study._semantic_fingerprint(command, parsed)
    second = provider_capability_study._semantic_fingerprint(command, parsed)

    assert first == second
    assert first is not None
    assert len(first) == 16
    assert "secret" not in first
    assert "Authorization" not in first


@settings(max_examples=20)
@given(missing_reason=st.sampled_from(["enabled", "api_key", "model", "base_url"]))
def test_agent_runtime_matrix_provider_is_skipped_when_base_provider_is_skipped(missing_reason: str) -> None:
    env_by_reason = {
        "enabled": "LIVE_ENV",
        "api_key": "API_KEY_ENV",
        "model": "MODEL_ENV",
        "base_url": "BASE_URL_ENV",
    }
    original_env = {env_name: os.environ.get(env_name) for env_name in env_by_reason.values()}
    scratch_root = REPO_ROOT / ".test-work" / "hypothesis-provider-plan"
    try:
        for env_name in env_by_reason.values():
            os.environ[env_name] = "present"
        os.environ.pop(env_by_reason[missing_reason], None)

        matrix = {
            "schema_version": "0.1",
            "name": "p0-property",
            "providers": [
                {
                    "name": "base",
                    "kind": "openai_compatible",
                    "capabilities": ["repair"],
                    "base_url_env": "BASE_URL_ENV",
                    "model_env": "MODEL_ENV",
                    "api_key_env": "API_KEY_ENV",
                    "enabled_env": "LIVE_ENV",
                    "live": True,
                },
                {
                    "name": "agent",
                    "kind": "agent_runtime",
                    "capabilities": ["repair"],
                    "base_provider": "base",
                    "live": True,
                    "enabled_env": "LIVE_ENV",
                },
            ],
        }

        plan = provider_capability_study.build_plan(matrix, matrix["providers"], scratch_root, include_live=True)

        base_skip = next(command for command in plan["commands"] if command["provider"] == "base")
        agent_skip = next(command for command in plan["commands"] if command["provider"] == "agent")
        assert base_skip["status"] == "skipped"
        assert agent_skip["status"] == "skipped"
        assert str(agent_skip["reason"]).startswith("base_provider_skipped:base:")

        dry_run = provider_capability_study.build_plan(matrix, matrix["providers"], scratch_root, include_live=True, dry_run=True)
        assert all(command["status"] == "planned" for command in dry_run["commands"])
    finally:
        for env_name, value in original_env.items():
            if value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = value
