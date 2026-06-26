from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from telchines.adapters.parsing import parse_common_output
from telchines.config import ProjectConfig
from telchines.models import ToolReference, VerificationRun
from telchines.run_store import RunStore
from telchines.utils import dataclass_to_dict, read_json, stable_id, utc_now
from telchines.waveforms import ingest_waveform


IMPORT_MANIFEST_SCHEMA_VERSION = "0.1"
IMPORT_WORKFLOW_TYPE = "regression_import"
VALID_IMPORTED_STATUSES = {"passed", "failed", "error", "skipped", "unknown"}


def import_regression_manifest(config: ProjectConfig, store: RunStore, manifest: Path, *, dry_run: bool = False) -> dict[str, object]:
    manifest_path = _resolve_manifest_file(config, manifest)
    payload = _load_manifest(manifest_path)
    _validate_manifest_header(payload)
    tool = _tool_reference(payload["tool"])
    run_items = payload["runs"]
    imported_at = utc_now()
    manifest_label = _label_path(config, manifest_path)

    imported_runs: list[dict[str, object]] = []
    for index, item in enumerate(run_items, start=1):
        imported = _build_imported_run(
            config,
            store,
            manifest_label=manifest_label,
            tool=tool,
            item=item,
            index=index,
            imported_at=imported_at,
            dry_run=dry_run,
        )
        imported_runs.append(imported)

    return {
        "schema_version": IMPORT_MANIFEST_SCHEMA_VERSION,
        "manifest_path": manifest_label,
        "dry_run": dry_run,
        "imported_count": len(imported_runs),
        "runs": imported_runs,
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("import manifest must be a JSON object")
    return payload


def _validate_manifest_header(payload: dict[str, Any]) -> None:
    schema_version = payload.get("schema_version")
    if schema_version != IMPORT_MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported import manifest schema_version: {schema_version!r}")
    if "tool" not in payload:
        raise ValueError("import manifest requires tool")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ValueError("import manifest runs must be a list")
    for index, item in enumerate(runs, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"import manifest run {index} must be an object")


def _tool_reference(value: object) -> ToolReference:
    if isinstance(value, str) and value.strip():
        return ToolReference(kind="regression_manager", name=value.strip())
    if isinstance(value, dict):
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("import manifest tool.name must be a non-empty string")
        kind = value.get("kind", "regression_manager")
        version = value.get("version", "unknown")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("import manifest tool.kind must be a non-empty string")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("import manifest tool.version must be a non-empty string")
        return ToolReference(kind=kind.strip(), name=name.strip(), version=version.strip())
    raise ValueError("import manifest tool must be a non-empty string or object")


def _build_imported_run(
    config: ProjectConfig,
    store: RunStore,
    *,
    manifest_label: str,
    tool: ToolReference,
    item: dict[str, Any],
    index: int,
    imported_at: str,
    dry_run: bool,
) -> dict[str, object]:
    name = _run_name(item, index)
    status = _run_status(item, index)
    metadata = _metadata(item, index)
    command = _command(item, index)
    log_paths = _path_list(config, item, index, field_names=("logs", "log_paths"))
    waveform_paths = _path_list(config, item, index, field_names=("waveforms", "waveform_paths"))
    artifact_paths = _artifact_paths(config, item.get("artifacts"), index)
    run_id = stable_id("run", config.project.project_id, IMPORT_WORKFLOW_TYPE, manifest_label, str(index), name, imported_at)

    observations = []
    for log_path in log_paths:
        observations.extend(parse_common_output(run_id, log_path.read_text(encoding="utf-8"), default_type="imported_log"))

    waveform_ids: list[str] = []
    if not dry_run:
        for waveform_path in waveform_paths:
            waveform_ids.append(ingest_waveform(config, store, waveform_path).waveform_id)
        store.save_observations(observations)

    relative_logs = [_label_path(config, path) for path in log_paths]
    relative_waveforms = [_label_path(config, path) for path in waveform_paths]
    relative_artifacts = {key: _label_path(config, path) for key, path in artifact_paths.items()}
    artifacts: dict[str, str] = {"manifest_path": manifest_label}
    if relative_logs:
        artifacts["log_paths"] = ",".join(relative_logs)
    if waveform_ids:
        artifacts["waveform_ids"] = ",".join(waveform_ids)
    for key, value in relative_artifacts.items():
        artifacts[_artifact_key(key, artifacts)] = value

    seed = item.get("seed")
    summary = item.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = f"Imported regression run {name} ({status})"
    tool_result = {
        "status": status,
        "imported": True,
        "manifest_path": manifest_label,
        "name": name,
        "seed": seed,
        "metadata": metadata,
        "log_paths": relative_logs,
        "artifact_paths": relative_artifacts,
        "waveform_paths": relative_waveforms,
    }
    run = VerificationRun(
        run_id=run_id,
        project_id=config.project.project_id,
        commit_sha=_string_value(item, "commit_sha", default="workspace"),
        workflow_type=IMPORT_WORKFLOW_TYPE,
        tool=tool,
        inputs={
            "manifest_path": manifest_label,
            "name": name,
            "seed": seed,
            "log_paths": relative_logs,
            "artifact_paths": relative_artifacts,
            "waveform_paths": relative_waveforms,
            "metadata": metadata,
        },
        status=status,
        started_at=_string_value(item, "started_at", default=imported_at),
        finished_at=_optional_string_value(item, "finished_at"),
        exit_code=_optional_int_value(item, "exit_code"),
        artifacts=artifacts,
        tool_result=tool_result,
        observation_ids=[observation.observation_id for observation in observations],
        summary=summary,
        replay_command=command,
    )
    if not dry_run:
        store.save_run(run)

    return {
        "run_id": run.run_id,
        "name": name,
        "status": status,
        "observation_count": len(observations),
        "waveform_count": len(waveform_paths),
        "log_paths": relative_logs,
        "artifact_paths": relative_artifacts,
        "stored": not dry_run,
        "run": dataclass_to_dict(run) if dry_run else None,
    }


def _run_name(item: dict[str, Any], index: int) -> str:
    for key in ("name", "id", "test"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"run_{index}"


def _run_status(item: dict[str, Any], index: int) -> str:
    value = item.get("status", "unknown")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"import manifest run {index} status must be a non-empty string")
    status = value.strip().lower()
    if status not in VALID_IMPORTED_STATUSES:
        raise ValueError(f"unsupported import manifest run {index} status: {value!r}")
    return status


def _metadata(item: dict[str, Any], index: int) -> dict[str, Any]:
    value = item.get("metadata", {})
    if not isinstance(value, dict):
        raise ValueError(f"import manifest run {index} metadata must be an object")
    return value


def _command(item: dict[str, Any], index: int) -> list[str]:
    value = item.get("command", [])
    if value in (None, ""):
        return []
    if not isinstance(value, list) or any(not isinstance(part, str) or not part.strip() for part in value):
        raise ValueError(f"import manifest run {index} command must be a list of non-empty strings")
    return value


def _path_list(config: ProjectConfig, item: dict[str, Any], index: int, *, field_names: tuple[str, ...]) -> list[Path]:
    value: object = []
    for field_name in field_names:
        if field_name in item:
            value = item[field_name]
            break
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, list) and all(isinstance(path, str) for path in value):
        raw_values = value
    else:
        raise ValueError(f"import manifest run {index} {field_names[0]} must be a string or list of strings")
    return [_resolve_project_file(config, raw, f"run {index} {field_names[0]}") for raw in raw_values]


def _artifact_paths(config: ProjectConfig, value: object, index: int) -> dict[str, Path]:
    if value in (None, ""):
        return {}
    if isinstance(value, list) and all(isinstance(path, str) for path in value):
        return {
            f"artifact_{artifact_index}": _resolve_project_file(config, raw, f"run {index} artifacts")
            for artifact_index, raw in enumerate(value, start=1)
        }
    if isinstance(value, dict):
        artifacts: dict[str, Path] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"import manifest run {index} artifact keys must be non-empty strings")
            if not isinstance(raw, str):
                raise ValueError(f"import manifest run {index} artifact {key} must be a string path")
            artifacts[key.strip()] = _resolve_project_file(config, raw, f"run {index} artifact {key}")
        return artifacts
    raise ValueError(f"import manifest run {index} artifacts must be a list or object")


def _resolve_manifest_file(config: ProjectConfig, manifest: Path) -> Path:
    candidate = manifest if manifest.is_absolute() else (config.project_root / manifest)
    resolved = candidate.resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"import manifest does not exist: {manifest}")
    return resolved


def _resolve_project_file(config: ProjectConfig, value: str, field_name: str) -> Path:
    raw = value.strip()
    if not raw:
        raise ValueError(f"{field_name} path must be non-empty")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ValueError(f"{field_name} path must be relative to the project root: {raw}")
    resolved = (config.project_root / candidate).resolve()
    try:
        resolved.relative_to(config.project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field_name} path escapes the project root: {raw}") from exc
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"{field_name} path does not exist: {raw}")
    return resolved


def _label_path(config: ProjectConfig, path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(config.project_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


def _artifact_key(key: str, existing: dict[str, str]) -> str:
    normalized = key.strip().replace(" ", "_")
    if normalized not in existing:
        return normalized
    prefixed = f"artifact_{normalized}"
    if prefixed not in existing:
        return prefixed
    index = 2
    while f"{prefixed}_{index}" in existing:
        index += 1
    return f"{prefixed}_{index}"


def _string_value(item: dict[str, Any], key: str, *, default: str) -> str:
    value = item.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"import manifest {key} must be a non-empty string")
    return value.strip()


def _optional_string_value(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"import manifest {key} must be a non-empty string when supplied")
    return value.strip()


def _optional_int_value(item: dict[str, Any], key: str) -> int | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"import manifest {key} must be an integer when supplied")
    return value
