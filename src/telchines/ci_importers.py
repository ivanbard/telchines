from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from telchines.config import ProjectConfig
from telchines.import_manifest import IMPORT_MANIFEST_SCHEMA_VERSION, import_regression_payload
from telchines.run_store import RunStore


SUPPORTED_CI_IMPORTERS = {"junit", "github-actions", "jenkins"}


def import_ci_runs(
    config: ProjectConfig,
    store: RunStore,
    source: Path,
    *,
    importer: str,
    dry_run: bool = False,
) -> dict[str, object]:
    source_path = _resolve_source(config, source)
    if importer == "junit":
        manifest = junit_to_manifest(source_path)
    elif importer == "github-actions":
        manifest = github_actions_to_manifest(source_path)
    elif importer == "jenkins":
        manifest = jenkins_to_manifest(source_path)
    else:
        raise ValueError(f"unsupported CI importer: {importer}")
    manifest_label = _label_path(config, source_path)
    payload = import_regression_payload(config, store, manifest, manifest_label=manifest_label, dry_run=dry_run)
    payload["source_format"] = importer
    return payload


def junit_to_manifest(source: Path) -> dict[str, Any]:
    root = ET.parse(source).getroot()
    suites = [root] if _strip_namespace(root.tag) == "testsuite" else [_strip_element(item) for item in root.iter() if _strip_namespace(item.tag) == "testsuite"]
    runs: list[dict[str, Any]] = []
    for suite in suites:
        suite_name = suite.attrib.get("name", source.stem)
        for index, testcase in enumerate([item for item in suite if _strip_namespace(item.tag) == "testcase"], start=1):
            case_name = testcase.attrib.get("name", f"case_{index}")
            class_name = testcase.attrib.get("classname", "")
            failure_nodes = [item for item in testcase if _strip_namespace(item.tag) in {"failure", "error"}]
            skipped = any(_strip_namespace(item.tag) == "skipped" for item in testcase)
            status = "failed" if failure_nodes else "skipped" if skipped else "passed"
            log_text = "\n".join(
                part
                for part in [
                    *(node.text or "" for node in failure_nodes),
                    _child_text(testcase, "system-out"),
                    _child_text(testcase, "system-err"),
                ]
                if part.strip()
            )
            runs.append(
                {
                    "name": ".".join(part for part in [class_name, case_name] if part),
                    "status": status,
                    "log_text": log_text,
                    "metadata": {
                        "suite": suite_name,
                        "classname": class_name,
                        "time": testcase.attrib.get("time", ""),
                        "source_format": "junit",
                    },
                }
            )
    return _manifest("junit", runs)


def github_actions_to_manifest(source: Path) -> dict[str, Any]:
    payload = _load_json_object(source)
    workflow_name = str(payload.get("workflow_name") or payload.get("name") or "github-actions")
    run_id = str(payload.get("run_id") or payload.get("databaseId") or "").strip()
    jobs_payload = payload.get("jobs", [])
    if isinstance(jobs_payload, dict):
        jobs_payload = jobs_payload.get("jobs", [])
    if not isinstance(jobs_payload, list):
        raise ValueError("github-actions import expects jobs to be a list or object with jobs")
    runs: list[dict[str, Any]] = []
    for index, job in enumerate(jobs_payload, start=1):
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or job.get("job_name") or f"job_{index}")
        conclusion = str(job.get("conclusion") or job.get("status") or "unknown").lower()
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            steps = []
        failing_steps = [step for step in steps if isinstance(step, dict) and str(step.get("conclusion") or step.get("status") or "").lower() in {"failure", "failed", "error"}]
        log_lines = [f"{name}: {conclusion}"]
        for step in failing_steps:
            log_lines.append(f"error: {name}: step {step.get('name', 'unnamed')} failed")
        annotations = job.get("annotations", [])
        if not isinstance(annotations, list):
            annotations = []
        for annotation in annotations:
            if isinstance(annotation, dict):
                path = annotation.get("path") or annotation.get("file")
                line = annotation.get("start_line") or annotation.get("line")
                message = annotation.get("message") or annotation.get("title") or ""
                if path and line and message:
                    log_lines.append(f"{path}:{line}: error: {message}")
        runs.append(
            {
                "name": name,
                "status": _ci_status(conclusion),
                "log_text": "\n".join(log_lines),
                "metadata": {
                    "workflow": workflow_name,
                    "run_id": run_id,
                    "job_id": job.get("id", ""),
                    "source_format": "github-actions",
                },
                "command": ["gh", "run", "rerun", run_id] if run_id else [],
            }
        )
    return _manifest("github-actions", runs)


def jenkins_to_manifest(source: Path) -> dict[str, Any]:
    payload = _load_json_object(source)
    builds = payload.get("builds")
    if not isinstance(builds, list):
        builds = [payload]
    runs: list[dict[str, Any]] = []
    for build_index, build in enumerate(builds, start=1):
        if not isinstance(build, dict):
            continue
        build_name = str(build.get("fullDisplayName") or build.get("displayName") or build.get("id") or f"build_{build_index}")
        build_result = str(build.get("result") or build.get("status") or "unknown").lower()
        cases = _jenkins_cases(build)
        if not cases:
            runs.append(
                {
                    "name": build_name,
                    "status": _ci_status(build_result),
                    "log_text": str(build.get("log") or build.get("description") or ""),
                    "metadata": {"build": build_name, "url": build.get("url", ""), "source_format": "jenkins"},
                }
            )
            continue
        for case_index, case in enumerate(cases, start=1):
            name = str(case.get("name") or case.get("fullName") or f"{build_name}_case_{case_index}")
            status = str(case.get("status") or "").lower()
            log_text = "\n".join(
                part
                for part in [str(case.get("errorDetails") or ""), str(case.get("errorStackTrace") or "")]
                if part.strip()
            )
            runs.append(
                {
                    "name": name,
                    "status": "failed" if status in {"failed", "regression", "error"} or log_text else "passed",
                    "log_text": log_text,
                    "metadata": {"build": build_name, "className": case.get("className", ""), "source_format": "jenkins"},
                }
            )
    return _manifest("jenkins", runs)


def _manifest(tool_name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": IMPORT_MANIFEST_SCHEMA_VERSION,
        "tool": {"kind": "regression_manager", "name": tool_name, "version": "normalized"},
        "runs": runs,
    }


def _load_json_object(source: Path) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{source.name} must contain a JSON object")
    return payload


def _resolve_source(config: ProjectConfig, source: Path) -> Path:
    candidate = source if source.is_absolute() else (config.project_root / source)
    resolved = candidate.resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"import source does not exist: {source}")
    return resolved


def _label_path(config: ProjectConfig, path: Path) -> str:
    try:
        return path.resolve().relative_to(config.project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _strip_element(element: ET.Element) -> ET.Element:
    return element


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _strip_namespace(child.tag) == name:
            return child.text or ""
    return ""


def _ci_status(value: str) -> str:
    lowered = value.lower()
    if lowered in {"success", "successful", "passed", "pass", "completed", "fixed"}:
        return "passed"
    if lowered in {"failure", "failed", "error", "unstable", "timed_out", "timed out"}:
        return "failed"
    if lowered in {"cancelled", "canceled", "skipped", "neutral"}:
        return "skipped"
    return "unknown"


def _jenkins_cases(build: dict[str, Any]) -> list[dict[str, Any]]:
    test_result = build.get("testResult")
    if not isinstance(test_result, dict):
        actions = build.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        for action in actions:
            if isinstance(action, dict) and isinstance(action.get("failCount"), int):
                test_result = action
                break
    if not isinstance(test_result, dict):
        return []
    cases: list[dict[str, Any]] = []
    suites = test_result.get("suites", [])
    if not isinstance(suites, list):
        suites = []
    for suite in suites:
        if isinstance(suite, dict):
            suite_cases = suite.get("cases", [])
            if not isinstance(suite_cases, list):
                suite_cases = []
            for case in suite_cases:
                if isinstance(case, dict):
                    cases.append(case)
    return cases
