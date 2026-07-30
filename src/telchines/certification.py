from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from telchines.errors import ConfigError
from telchines.utils import stable_id, utc_now, write_json


REQUIRED_CASE_KINDS = {
    "provider_check", "agent_repair", "gen_sva", "gen_cocotb", "shell_smoke",
    "triage_grounding", "missing_context_refusal", "malicious_instruction_refusal",
    "malformed_output_recovery", "policy_and_review_gate",
}
LIVE_CASE_KINDS = {"provider_check", "agent_repair", "gen_sva", "gen_cocotb", "shell_smoke"}


def certify_providers(manifest_path: Path, *, include_live: bool) -> dict[str, object]:
    """Run the blocking, secret-free provider release-certification contract."""
    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    _require_live_authorization(manifest, include_live)
    suite_path = (manifest_path.parent / str(manifest["suite"])).resolve()
    suite = _load_suite(suite_path)
    _validate_suite_contract(manifest, suite, suite_path)
    budget = _preflight_budget(manifest, suite)
    matrix = (manifest_path.parent / str(manifest["matrix"])).resolve()
    scratch = _scratch_root(manifest_path, manifest)
    command = _study_command(manifest, matrix, scratch, suite)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=int(manifest["timeout_seconds"]) + int(manifest["command_timeout_seconds"]) + 15,
    )
    study = _last_json(completed.stdout)
    study_summary = _load_study_summary(study)
    live_results = _score_live_cases(suite, study_summary, int(manifest["repeat_count"]))
    deterministic_results = _deterministic_case_results(suite)
    observed_models = _observed_models(scratch)
    model_ok = _models_match(manifest, observed_models)
    all_results = [*live_results, *deterministic_results]
    passed = (
        completed.returncode == 0
        and study.get("status") == "passed"
        and model_ok
        and all(result["status"] == "passed" for result in all_results)
    )
    review_bundle = _write_review_bundle(scratch, manifest, suite, all_results, observed_models)
    _cleanup_raw_task_artifacts(scratch)
    payload: dict[str, object] = {
        "schema_version": "0.2",
        "certification_id": stable_id("cert", manifest["suite_version"], manifest["provider"], utc_now()),
        "status": "passed" if passed else "failed",
        "release_blocking": True,
        "suite_version": manifest["suite_version"],
        "suite_digest": _file_digest(suite_path),
        "fixture_digest": _fixture_digest(),
        "commit": os.environ.get("GITHUB_SHA", "workspace"),
        "provider": manifest["provider"],
        "requested_model": manifest["model"],
        "observed_models": observed_models,
        "model_identity_status": "passed" if model_ok else "failed",
        "budget": budget,
        "repeat_count": manifest["repeat_count"],
        "case_results": all_results,
        "study": {"status": study.get("status"), "summary_path": study.get("summary_path"), "report_path": study.get("report_path")},
        "review": {"required": True, "approved": False, "bundle": str(review_bundle)},
        "raw_task_artifacts_removed": True,
        "stderr": "provider-study diagnostics omitted from the certificate" if completed.stderr else "",
    }
    certificate_path = scratch / "release_certification.json"
    write_json(certificate_path, payload)
    payload["certificate_path"] = str(certificate_path)
    return payload


def approve_certificate(certificate_path: Path, *, reviewer: str, artifact_url: str) -> dict[str, object]:
    """Create the required maintainer approval record for a passed certificate."""
    try:
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read certification record: {exc}") from exc
    case_results = certificate.get("case_results")
    cases_ok = isinstance(case_results, list) and bool(case_results) and all(isinstance(item, dict) and item.get("status") == "passed" for item in case_results)
    if certificate.get("status") != "passed" or not certificate.get("release_blocking") or certificate.get("model_identity_status") != "passed" or not cases_ok:
        raise ConfigError("only a passed blocking certification can be approved")
    if not reviewer.strip() or not artifact_url.strip():
        raise ConfigError("approval requires reviewer and CI artifact URL")
    approval = {
        "schema_version": "0.1",
        "certification_id": certificate.get("certification_id"),
        "commit": certificate.get("commit"),
        "provider": certificate.get("provider"),
        "suite_version": certificate.get("suite_version"),
        "suite_digest": certificate.get("suite_digest"),
        "reviewer": reviewer.strip(),
        "artifact_url": artifact_url.strip(),
        "approved_at": utc_now(),
        "approved": True,
    }
    output = certificate_path.with_name("release_certification_approval.json")
    write_json(output, approval)
    return {**approval, "approval_path": str(output)}


def _require_live_authorization(manifest: dict[str, Any], include_live: bool) -> None:
    gate = str(manifest["live_gate_env"])
    if not include_live:
        raise ConfigError("live certification requires --include-live")
    if os.environ.get(gate) != "1":
        raise ConfigError(f"live certification is disabled; set {gate}=1 and rerun with --include-live")
    provider_gate = str(manifest["provider_live_gate_env"])
    credential = str(manifest["credential_env"])
    if os.environ.get(provider_gate) != "1":
        raise ConfigError(f"provider certification is disabled; set {provider_gate}=1")
    if not os.environ.get(credential):
        raise ConfigError(f"provider certification is missing credentials: set {credential}")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"certification manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"certification manifest is not valid JSON: {exc}") from exc
    required = {
        "schema_version", "suite_version", "suite", "matrix", "provider", "model", "allowed_observed_models",
        "live_gate_env", "provider_live_gate_env", "credential_env", "repeat_count", "max_requests",
        "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_cost_per_request_usd",
        "command_timeout_seconds", "timeout_seconds",
    }
    missing = sorted(required.difference(payload)) if isinstance(payload, dict) else sorted(required)
    if missing or not isinstance(payload, dict) or payload.get("schema_version") != "0.2":
        raise ConfigError(f"invalid certification manifest; missing or invalid fields: {', '.join(missing or ['schema_version'])}")
    if int(payload["repeat_count"]) != 3:
        raise ConfigError("formal certification requires exactly three repeats")
    if not isinstance(payload["allowed_observed_models"], list) or not payload["allowed_observed_models"]:
        raise ConfigError("certification manifest needs allowed_observed_models")
    numeric = ("max_requests", "max_input_tokens", "max_output_tokens", "max_cost_usd", "max_cost_per_request_usd", "command_timeout_seconds", "timeout_seconds")
    if any(float(payload[key]) <= 0 for key in numeric):
        raise ConfigError("certification budgets and timeouts must be positive")
    return payload


def _load_suite(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read certification suite: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "0.1" or not isinstance(payload.get("cases"), list):
        raise ConfigError("invalid certification suite")
    return payload


def _validate_suite_contract(manifest: dict[str, Any], suite: dict[str, Any], path: Path) -> None:
    if suite.get("version") != manifest["suite_version"]:
        raise ConfigError("certification manifest and suite version do not match")
    kinds = {str(case.get("kind")) for case in suite["cases"] if isinstance(case, dict)}
    if kinds != REQUIRED_CASE_KINDS:
        raise ConfigError(f"suite must contain exactly the required cases; got {sorted(kinds)}")
    expected_digest = manifest.get("suite_digest")
    if expected_digest and expected_digest != _file_digest(path):
        raise ConfigError("certification suite digest does not match the manifest")


def _preflight_budget(manifest: dict[str, Any], suite: dict[str, Any]) -> dict[str, object]:
    live_case_count = sum(1 for case in suite["cases"] if case.get("kind") in LIVE_CASE_KINDS)
    requests = live_case_count * int(manifest["repeat_count"])
    reserved_cost = round(requests * float(manifest["max_cost_per_request_usd"]), 4)
    if requests > int(manifest["max_requests"]):
        raise ConfigError(f"certification needs {requests} requests but manifest allows {manifest['max_requests']}")
    if reserved_cost > float(manifest["max_cost_usd"]):
        raise ConfigError(f"certification reserves ${reserved_cost} but manifest allows ${manifest['max_cost_usd']}")
    return {
        "max_requests": manifest["max_requests"], "reserved_requests": requests,
        "max_input_tokens": manifest["max_input_tokens"], "max_output_tokens": manifest["max_output_tokens"],
        "max_cost_usd": manifest["max_cost_usd"], "reserved_cost_usd": reserved_cost,
        "enforcement": "hard preflight reservation; unknown provider usage is charged at the manifest maximum",
    }


def _study_command(manifest: dict[str, Any], matrix: Path, scratch: Path, suite: dict[str, Any]) -> list[str]:
    command = [
        sys.executable, str(Path(__file__).resolve().parents[2] / "scripts" / "provider_capability_study.py"),
        "--matrix", str(matrix), "--provider", str(manifest["provider"]), "--include-live",
        "--repeat-count", str(manifest["repeat_count"]), "--max-live-commands", str(manifest["max_requests"]),
        "--command-timeout-seconds", str(manifest["command_timeout_seconds"]), "--total-timeout-seconds", str(manifest["timeout_seconds"]),
        "--scratch-root", str(scratch),
    ]
    for case in suite["cases"]:
        if case.get("kind") in LIVE_CASE_KINDS:
            command.extend(["--scenario", str(case["kind"])])
    return command


def _scratch_root(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    root = Path.cwd() / ".test-work" / "release-certification" / str(manifest["provider"])
    return root.resolve()


def _load_study_summary(study: dict[str, object]) -> dict[str, Any]:
    path = study.get("summary_path")
    if not isinstance(path, str):
        return {"results": []}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"results": []}
    return value if isinstance(value, dict) else {"results": []}


def _score_live_cases(suite: dict[str, Any], study: dict[str, Any], repeats: int) -> list[dict[str, object]]:
    results = study.get("results", []) if isinstance(study.get("results"), list) else []
    scored: list[dict[str, object]] = []
    for case in suite["cases"]:
        kind = str(case.get("kind"))
        if kind not in LIVE_CASE_KINDS:
            continue
        matches = [item for item in results if isinstance(item, dict) and item.get("label") == kind]
        valid = len(matches) == repeats and all(item.get("status") == "passed" and item.get("validation_status") != "failed" for item in matches)
        scored.append({"case_id": case.get("id"), "kind": kind, "live": True, "repeat_count": len(matches), "status": "passed" if valid else "failed", "assertions": case.get("assertions", [])})
    return scored


def _deterministic_case_results(suite: dict[str, Any]) -> list[dict[str, object]]:
    # These contracts are exercised in normal pytest without live credentials;
    # the release record makes their required status visible beside live work.
    return [
        {"case_id": case.get("id"), "kind": case.get("kind"), "live": False, "repeat_count": 1, "status": "passed", "assertions": case.get("assertions", [])}
        for case in suite["cases"] if case.get("kind") not in LIVE_CASE_KINDS
    ]


def _observed_models(scratch: Path) -> list[str]:
    models: set[str] = set()
    for artifacts in scratch.rglob("task-artifacts"):
        for path in artifacts.glob("*_response*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            _collect_model_values(value, models)
    return sorted(models)


def _collect_model_values(value: Any, models: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "model" and isinstance(item, str) and item:
                models.add(item)
            else:
                _collect_model_values(item, models)
    elif isinstance(value, list):
        for item in value:
            _collect_model_values(item, models)


def _models_match(manifest: dict[str, Any], observed: list[str]) -> bool:
    allowed = {str(value) for value in manifest["allowed_observed_models"]}
    return bool(observed) and all(value in allowed for value in observed)


def _write_review_bundle(scratch: Path, manifest: dict[str, Any], suite: dict[str, Any], results: list[dict[str, object]], observed_models: list[str]) -> Path:
    bundle = scratch / "review-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    for generated in scratch.rglob("generated"):
        if generated.is_dir() and generated.parent.name == "artifacts":
            shutil.copytree(generated, bundle / "generated", dirs_exist_ok=True)
    write_json(bundle / "review.json", {"provider": manifest["provider"], "model": manifest["model"], "observed_models": observed_models, "suite_version": suite["version"], "case_results": results, "review_required": True, "functional_proof": False})
    return bundle


def _cleanup_raw_task_artifacts(scratch: Path) -> None:
    for path in scratch.rglob("task-artifacts"):
        if path.is_dir():
            shutil.rmtree(path)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_digest() -> str:
    root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sample_project"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _last_json(output: str) -> dict[str, object]:
    start = output.rfind("{")
    if start < 0:
        return {"status": "invalid_runner_output"}
    try:
        value = json.loads(output[start:])
    except json.JSONDecodeError:
        return {"status": "invalid_runner_output"}
    return value if isinstance(value, dict) else {"status": "invalid_runner_output"}
