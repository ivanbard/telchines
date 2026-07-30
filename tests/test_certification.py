from __future__ import annotations

import json
from pathlib import Path

import pytest

from telchines.certification import (
    _deterministic_case_results,
    _file_digest,
    _load_manifest,
    _load_suite,
    _models_match,
    _cleanup_raw_task_artifacts,
    _observed_models,
    _write_review_bundle,
    _preflight_budget,
    _validate_suite_contract,
    approve_certificate,
)
from telchines.errors import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[1]


def _manifest(name: str = "openai") -> dict[str, object]:
    return _load_manifest(REPO_ROOT / "docs" / "provider-certifications" / f"{name}.json")


def _suite() -> dict[str, object]:
    return _load_suite(REPO_ROOT / "docs" / "certification-suites" / "llm-certification-v2.json")


def test_v2_suite_is_complete_and_matches_manifest_digest() -> None:
    manifest = _manifest()
    suite_path = REPO_ROOT / "docs" / "certification-suites" / "llm-certification-v2.json"

    _validate_suite_contract(manifest, _suite(), suite_path)

    assert manifest["suite_digest"] == _file_digest(suite_path)
    assert len(_deterministic_case_results(_suite())) == 5


def test_preflight_reserves_all_live_cases_and_rejects_over_budget() -> None:
    manifest = _manifest()
    budget = _preflight_budget(manifest, _suite())

    assert budget["reserved_requests"] == 15
    assert budget["reserved_cost_usd"] == 7.5
    too_small = {**manifest, "max_cost_usd": 1.0}
    with pytest.raises(ConfigError, match="reserves"):
        _preflight_budget(too_small, _suite())


def test_model_identity_requires_an_observed_allowed_model() -> None:
    manifest = _manifest()
    assert _models_match(manifest, ["gpt-5.5-2026-04-23"])
    assert not _models_match(manifest, [])
    assert not _models_match(manifest, ["unexpected-model"])


def test_approval_requires_a_passed_blocking_certificate(work_root: Path) -> None:
    certificate = work_root / "certificate.json"
    certificate.write_text(json.dumps({"status": "passed", "release_blocking": True, "model_identity_status": "passed", "case_results": [{"status": "passed"}], "certification_id": "cert_1", "provider": "openai"}), encoding="utf-8")

    approval = approve_certificate(certificate, reviewer="maintainer", artifact_url="https://ci.example/artifacts/1")

    assert approval["approved"] is True
    assert Path(str(approval["approval_path"])).exists()
    certificate.write_text(json.dumps({"status": "failed", "release_blocking": True}), encoding="utf-8")
    with pytest.raises(ConfigError, match="passed blocking"):
        approve_certificate(certificate, reviewer="maintainer", artifact_url="https://ci.example/artifacts/1")


def test_review_bundle_uses_nested_study_artifacts_and_removes_raw_responses(work_root: Path) -> None:
    nested = work_root / "openai" / ".tel"
    artifacts = nested / "task-artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "task_response.json").write_text(json.dumps({"response": {"model": "gpt-5.5-2026-04-23"}}), encoding="utf-8")
    generated = nested / "artifacts" / "generated"
    generated.mkdir(parents=True)
    (generated / "draft.sv").write_text("module draft; endmodule\n", encoding="utf-8")

    assert _observed_models(work_root) == ["gpt-5.5-2026-04-23"]
    bundle = _write_review_bundle(work_root, _manifest(), _suite(), [], ["gpt-5.5-2026-04-23"])
    _cleanup_raw_task_artifacts(work_root)

    assert (bundle / "generated" / "draft.sv").exists()
    assert not artifacts.exists()
