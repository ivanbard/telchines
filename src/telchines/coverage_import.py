from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from telchines.config import ProjectConfig
from telchines.utils import ensure_directory, read_json, utc_now, write_json
from telchines.workflows.coverage import load_coverage_report


SUPPORTED_COVERAGE_FORMATS = {"telchines-json", "ucis-json", "vivado", "quartus", "questa-text"}


def import_coverage_report(config: ProjectConfig, source: Path, *, source_format: str, output: Path) -> dict[str, object]:
    source_path = _resolve_source(config, source)
    output_path = output if output.is_absolute() else (config.project_root / output).resolve()
    if source_format == "telchines-json":
        normalized = _normalize_telchines_json(source_path)
    elif source_format == "ucis-json":
        normalized = _normalize_ucis_json(source_path)
    elif source_format in {"vivado", "quartus", "questa-text"}:
        normalized = _normalize_text_report(source_path, source_format)
    else:
        raise ValueError(f"unsupported coverage format: {source_format}")
    ensure_directory(output_path.parent)
    write_json(output_path, normalized)
    load_coverage_report(output_path)
    return {
        "status": "imported",
        "source_path": _label_path(config, source_path),
        "source_format": source_format,
        "output_path": _label_path(config, output_path),
        "item_count": len(normalized["items"]),
        "excluded_count": len(normalized.get("exclusions", [])),
        "warning_count": len(normalized.get("import_warnings", [])),
        "import_warnings": normalized.get("import_warnings", []),
    }


def _normalize_telchines_json(source: Path) -> dict[str, Any]:
    report = read_json(source)
    if not isinstance(report, dict):
        raise ValueError("telchines-json coverage source must be a JSON object")
    load_coverage_report(source)
    report.setdefault("import_warnings", [])
    return report


def _normalize_ucis_json(source: Path) -> dict[str, Any]:
    payload = read_json(source)
    if not isinstance(payload, dict):
        raise ValueError("ucis-json coverage source must be a JSON object")
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    focus_paths: list[str] = []
    for item in payload.get("items", []):
        if isinstance(item, dict):
            items.append(_coverage_item_from_object(item, warnings))
    for covergroup in payload.get("covergroups", []):
        if not isinstance(covergroup, dict):
            warnings.append("ignored non-object covergroup")
            continue
        module = str(covergroup.get("module") or covergroup.get("scope") or covergroup.get("name") or "unknown").replace(".", "_")
        for coverpoint in covergroup.get("coverpoints", []):
            if not isinstance(coverpoint, dict):
                warnings.append(f"ignored non-object coverpoint in {module}")
                continue
            metric = str(coverpoint.get("metric") or "functional")
            cp_name = str(coverpoint.get("name") or "coverpoint")
            source_path = str(coverpoint.get("source") or coverpoint.get("file") or "").replace("\\", "/")
            if source_path:
                focus_paths.append(source_path)
            bins = coverpoint.get("bins", [])
            if not isinstance(bins, list) or not bins:
                items.append(
                    _item(
                        item_id=f"{module}_{cp_name}",
                        module=module,
                        metric=metric,
                        name=cp_name,
                        hits=int(coverpoint.get("hits", 0) or 0),
                        goal=int(coverpoint.get("goal", 1) or 1),
                        detail=str(coverpoint.get("detail") or "Imported UCIS coverpoint."),
                    )
                )
                continue
            for bin_item in bins:
                if not isinstance(bin_item, dict):
                    warnings.append(f"ignored non-object bin in {module}.{cp_name}")
                    continue
                bin_name = str(bin_item.get("name") or "bin")
                items.append(
                    _item(
                        item_id=f"{module}_{cp_name}_{bin_name}",
                        module=module,
                        metric=metric,
                        name=f"{cp_name}.{bin_name}",
                        hits=int(bin_item.get("count", bin_item.get("hits", 0)) or 0),
                        goal=int(bin_item.get("at_least", bin_item.get("goal", 1)) or 1),
                        detail=str(bin_item.get("detail") or "Imported UCIS bin."),
                    )
                )
    exclusions = [_exclusion(item, warnings) for item in payload.get("exclusions", []) if isinstance(item, dict)]
    if not items:
        raise ValueError("ucis-json coverage source did not contain any coverage items")
    return {
        "schema_version": "0.1",
        "tool": str(payload.get("tool") or "ucis-json"),
        "generated_at": str(payload.get("generated_at") or utc_now()),
        "design": str(payload.get("design") or payload.get("name") or "unknown_design"),
        "items": items,
        "focus_paths": _unique([*focus_paths, *[str(path).replace("\\", "/") for path in payload.get("focus_paths", []) if isinstance(path, str)]]),
        "exclusions": exclusions,
        "reachability_hints": payload.get("reachability_hints", []) if isinstance(payload.get("reachability_hints", []), list) else [],
        "import_warnings": warnings,
    }


def _normalize_text_report(source: Path, source_format: str) -> dict[str, Any]:
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    focus_paths: list[str] = []
    design = source.stem
    for line_number, line in enumerate(source.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        parsed = _parse_text_coverage_line(line)
        if parsed is None:
            if line.strip() and any(token in line.lower() for token in ("cover", "coverage", "assert", "fsm")):
                warnings.append(f"line {line_number}: unsupported coverage line")
            continue
        parsed_source = parsed.pop("source_path", "")
        if parsed_source:
            focus_paths.append(parsed_source)
        items.append(_item(**parsed))
    if not items:
        raise ValueError(f"{source_format} coverage source did not contain any supported coverage lines")
    return {
        "schema_version": "0.1",
        "tool": source_format,
        "generated_at": utc_now(),
        "design": design,
        "items": items,
        "focus_paths": _unique(focus_paths),
        "exclusions": [],
        "reachability_hints": [],
        "import_warnings": warnings,
    }


def _parse_text_coverage_line(line: str) -> dict[str, Any] | None:
    patterns = [
        re.compile(
            r"^(?:COVERAGE|Coverage|coverage):\s*(?P<module>[A-Za-z_][\w$]*)\s+"
            r"(?P<metric>functional|assertion|code|fsm)\s+(?P<name>[A-Za-z_][\w$.]*)\s+"
            r"(?P<hits>\d+)\s*/\s*(?P<goal>\d+)(?:\s+(?P<source_path>\S+))?"
        ),
        re.compile(
            r"^(?:Coverpoint|ASSERTION|Assertion|FSM|Code)\s+"
            r"(?P<module>[A-Za-z_][\w$]*)[.:](?P<name>[A-Za-z_][\w$.]*)\s+"
            r"(?P<hits>\d+)\s*/\s*(?P<goal>\d+)(?:\s+(?P<source_path>\S+))?",
            re.IGNORECASE,
        ),
    ]
    for pattern in patterns:
        match = pattern.search(line.strip())
        if not match:
            continue
        groups = match.groupdict()
        metric = groups.get("metric")
        if not metric:
            lowered = line.lower()
            metric = "assertion" if "assert" in lowered else "fsm" if "fsm" in lowered else "code" if "code" in lowered else "functional"
        module = groups["module"]
        name = groups["name"]
        return {
            "item_id": f"{module}_{name}".replace(".", "_"),
            "module": module,
            "metric": metric,
            "name": name,
            "hits": int(groups["hits"]),
            "goal": int(groups["goal"]),
            "detail": f"Imported coverage line: {line.strip()}",
            "source_path": str(groups.get("source_path") or "").replace("\\", "/"),
        }
    return None


def _coverage_item_from_object(item: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    try:
        return _item(
            item_id=str(item.get("item_id") or item["name"]),
            module=str(item.get("module") or item.get("scope") or "unknown"),
            metric=str(item.get("metric") or item.get("type") or "functional"),
            name=str(item.get("name") or item.get("item_id")),
            hits=int(item.get("hits", item.get("count", 0)) or 0),
            goal=int(item.get("goal", item.get("at_least", 1)) or 1),
            detail=str(item.get("detail") or "Imported UCIS item."),
        )
    except KeyError:
        warnings.append("ignored UCIS item missing name/item_id")
        return _item(item_id="ignored_invalid_item", module="unknown", metric="functional", name="ignored_invalid_item", hits=0, goal=1, detail="Invalid imported item.")


def _item(*, item_id: str, module: str, metric: str, name: str, hits: int, goal: int, detail: str) -> dict[str, Any]:
    goal = max(int(goal), 1)
    hits = max(int(hits), 0)
    return {
        "item_id": _clean_identifier(item_id),
        "module": module.strip() or "unknown",
        "metric": metric.strip().lower() or "functional",
        "name": name.strip() or _clean_identifier(item_id),
        "hits": hits,
        "goal": goal,
        "coverage": round(hits / goal, 3),
        "detail": detail.strip(),
    }


def _exclusion(item: dict[str, Any], warnings: list[str]) -> dict[str, str]:
    item_id = str(item.get("item_id") or item.get("id") or "").strip()
    if not item_id:
        warnings.append("ignored coverage exclusion missing item_id")
        item_id = "ignored_invalid_exclusion"
    return {
        "item_id": _clean_identifier(item_id),
        "reason": str(item.get("reason") or "Imported exclusion.").strip(),
        "status": str(item.get("status") or "active").strip() or "active",
    }


def _resolve_source(config: ProjectConfig, source: Path) -> Path:
    candidate = source if source.is_absolute() else (config.project_root / source)
    resolved = candidate.resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"coverage source does not exist: {source}")
    return resolved


def _label_path(config: ProjectConfig, path: Path) -> str:
    try:
        return path.resolve().relative_to(config.project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _clean_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]+", "_", value.strip()).strip("_")
    return cleaned or "coverage_item"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
