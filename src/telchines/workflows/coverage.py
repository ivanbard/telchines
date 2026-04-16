from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from telchines.config import ProjectConfig
from telchines.models import (
    AgentTask,
    CoverageExclusion,
    CoverageItem,
    CoveragePlan,
    CoverageReachabilityHint,
    CoverageRecommendation,
    CoverageReport,
    ToolReference,
    VerificationRun,
)
from telchines.retrieval import RetrievalContext, RetrievalHit, RetrievalService
from telchines.run_store import RunStore
from telchines.utils import dataclass_to_dict, read_json, relative_to, stable_id, utc_now


def execute_coverage_plan(
    config: ProjectConfig,
    store: RunStore,
    retrieval: RetrievalService,
    report_path: Path,
    *,
    exclusions_path: Path | None = None,
    formal_run_id: str | None = None,
    rtl_paths: list[Path] | None = None,
    spec_paths: list[Path] | None = None,
) -> tuple[CoveragePlan, VerificationRun, RetrievalContext]:
    report_rel = relative_to(report_path, config.project_root)
    exclusions_rel = relative_to(exclusions_path, config.project_root) if exclusions_path else None
    report = load_coverage_report(report_path)
    external_exclusions = load_coverage_exclusions(exclusions_path) if exclusions_path else []
    if external_exclusions:
        report.exclusions = [*report.exclusions, *external_exclusions]

    rtl_rel = [relative_to(path, config.project_root) for path in (rtl_paths or [])]
    spec_rel = [relative_to(path, config.project_root) for path in (spec_paths or [])]
    focus_paths = _unique([*report.focus_paths, *rtl_rel, *spec_rel])
    overall_query = _coverage_query(report)
    context = retrieval.search(overall_query, mode="coverage", focus_paths=focus_paths)
    store.save_context(context)

    task = AgentTask(
        task_id=stable_id("task", config.project.project_id, "coverage_plan", report_rel, formal_run_id or "", utc_now()),
        project_id=config.project.project_id,
        workflow_type="coverage_plan",
        input_run_id=formal_run_id,
        status="running",
        created_at=utc_now(),
        metadata={
            "report_path": report_rel,
            "exclusions_path": exclusions_rel,
            "formal_run_id": formal_run_id,
            "focus_paths": focus_paths,
        },
    )
    store.save_task(task)

    formal_run = _load_formal_run(store, formal_run_id)
    recommendations, excluded_item_ids = _build_recommendations(report, context, formal_run)
    plan = CoveragePlan(
        plan_id=stable_id("coverage", task.task_id, report_rel),
        task_id=task.task_id,
        report_path=report_rel,
        exclusions_path=exclusions_rel,
        formal_run_id=formal_run_id,
        summary=_plan_summary(report.design, recommendations, excluded_item_ids),
        focus_paths=focus_paths,
        recommendations=recommendations,
        excluded_item_ids=excluded_item_ids,
    )

    normalized_report_artifact = store.save_task_artifact(task.task_id, "coverage_report_normalized", _serialize_report(report))
    evidence_artifact = store.save_task_artifact(
        task.task_id,
        "coverage_evidence",
        {
            "context_id": context.context_id,
            "hits": [asdict(hit) for hit in context.hits],
            "formal_run_id": formal_run_id,
        },
    )
    plan_artifact = store.save_task_artifact(task.task_id, "coverage_plan", dataclass_to_dict(plan))

    run = VerificationRun(
        run_id=stable_id("run", plan.plan_id, utc_now()),
        project_id=config.project.project_id,
        commit_sha="workspace",
        workflow_type="coverage_plan",
        tool=ToolReference(kind="planner", name="coverage_assistant", version="0.1"),
        inputs={
            "report_path": report_rel,
            "exclusions_path": exclusions_rel,
            "formal_run_id": formal_run_id,
            "rtl_paths": rtl_rel,
            "spec_paths": spec_rel,
        },
        status="passed",
        started_at=utc_now(),
        finished_at=utc_now(),
        exit_code=0,
        artifacts={
            "normalized_report_artifact": str(normalized_report_artifact),
            "coverage_plan_artifact": str(plan_artifact),
            "coverage_evidence_artifact": str(evidence_artifact),
        },
        tool_result={
            "status": "planned",
            "report_source": report_rel,
            "recommendation_count": len(recommendations),
            "excluded_count": len(excluded_item_ids),
            "formal_run_id": formal_run_id,
            "classifications": [item.classification for item in recommendations[:3]],
        },
        summary=plan.summary,
    )
    store.save_run(run)

    task.status = "planned"
    task.metadata.update(
        {
            "context_id": context.context_id,
            "plan_id": plan.plan_id,
            "run_id": run.run_id,
            "plan_artifact": str(plan_artifact),
            "report_artifact": str(normalized_report_artifact),
        }
    )
    store.save_task(task)
    return plan, run, context


def load_coverage_report(path: Path) -> CoverageReport:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("coverage report must be a JSON object")
    tool = _required_str(payload, "tool")
    generated_at = _required_str(payload, "generated_at")
    design = _required_str(payload, "design")
    items_payload = payload.get("items")
    if not isinstance(items_payload, list) or not items_payload:
        raise ValueError("coverage report must contain a non-empty items array")
    items = [_parse_item(item) for item in items_payload]
    focus_paths = _parse_string_list(payload.get("focus_paths", []), "focus_paths")
    exclusions = [_parse_exclusion(item) for item in _parse_object_list(payload.get("exclusions", []), "exclusions")]
    reachability_hints = [_parse_reachability_hint(item) for item in _parse_object_list(payload.get("reachability_hints", []), "reachability_hints")]
    return CoverageReport(
        tool=tool,
        generated_at=generated_at,
        design=design,
        items=items,
        focus_paths=focus_paths,
        exclusions=exclusions,
        reachability_hints=reachability_hints,
        schema_version=str(payload.get("schema_version", "0.1")),
    )


def load_coverage_exclusions(path: Path) -> list[CoverageExclusion]:
    payload = read_json(path)
    exclusions_payload: Any
    if isinstance(payload, dict):
        exclusions_payload = payload.get("exclusions", [])
    else:
        exclusions_payload = payload
    return [_parse_exclusion(item) for item in _parse_object_list(exclusions_payload, "exclusions")]


def format_coverage_human(payload: dict[str, object]) -> str:
    recommendations = payload.get("recommendations", [])
    lines = [f"run {payload['run_id']} produced {payload['recommendation_count']} recommendation(s)"]
    if payload.get("excluded_count"):
        lines.append(f"excluded items skipped: {payload['excluded_count']}")
    for index, recommendation in enumerate(recommendations[:5], start=1):
        evidence = ", ".join(recommendation.get("evidence_citations", [])[:2]) or "none"
        lines.extend(
            [
                "",
                f"{index}. {recommendation['module']}::{recommendation['name']}",
                f"classification: {recommendation['classification']}",
                f"priority: {recommendation['priority']}",
                f"action: {recommendation['suggested_action']}",
                f"rationale: {recommendation['rationale']}",
                f"confidence: {recommendation['confidence']}",
                f"evidence: {evidence}",
            ]
        )
    return "\n".join(lines)


def _serialize_report(report: CoverageReport) -> dict[str, object]:
    return {
        "tool": report.tool,
        "generated_at": report.generated_at,
        "design": report.design,
        "items": [dataclass_to_dict(item) for item in report.items],
        "focus_paths": report.focus_paths,
        "exclusions": [dataclass_to_dict(item) for item in report.exclusions],
        "reachability_hints": [dataclass_to_dict(item) for item in report.reachability_hints],
        "schema_version": report.schema_version,
    }


def _build_recommendations(
    report: CoverageReport,
    context: RetrievalContext,
    formal_run: VerificationRun | None,
) -> tuple[list[CoverageRecommendation], list[str]]:
    exclusions = {item.item_id: item for item in report.exclusions if item.status.lower() == "active"}
    hints = {item.item_id: item for item in report.reachability_hints}
    recommendations: list[CoverageRecommendation] = []
    excluded_item_ids: list[str] = []
    for item in sorted(report.items, key=_priority_sort_key):
        if item.item_id in exclusions:
            excluded_item_ids.append(item.item_id)
            continue
        reachability_status, supporting_run_ids = _reachability_for_item(item, hints.get(item.item_id), formal_run)
        classification = _classify_item(item, reachability_status)
        priority = _priority_for_item(item, classification)
        evidence_hits = _select_evidence_hits(item, context.hits)
        recommendations.append(
            CoverageRecommendation(
                item_id=item.item_id,
                module=item.module,
                metric=item.metric,
                name=item.name,
                classification=classification,
                priority=priority,
                suggested_action=_suggested_action(item, classification),
                rationale=_rationale(item, classification, reachability_status, supporting_run_ids),
                confidence=_confidence(classification, evidence_hits, reachability_status, supporting_run_ids),
                evidence_citations=[hit.citation for hit in evidence_hits],
                evidence_paths=[hit.path for hit in evidence_hits],
                supporting_run_ids=supporting_run_ids,
                reachability_status=reachability_status,
            )
        )
    recommendations.sort(key=lambda item: (-item.priority, item.module, item.name))
    return recommendations[:8], excluded_item_ids


def _plan_summary(design: str, recommendations: list[CoverageRecommendation], excluded_item_ids: list[str]) -> str:
    if not recommendations:
        return f"{design}: no actionable uncovered items remain after exclusions"
    top = recommendations[0]
    return (
        f"{design}: planned {len(recommendations)} coverage actions; "
        f"top item {top.module}::{top.name} classified as {top.classification}"
        + (f"; skipped {len(excluded_item_ids)} excluded item(s)" if excluded_item_ids else "")
    )


def _coverage_query(report: CoverageReport) -> str:
    terms: list[str] = [report.design, "coverage closure"]
    for item in sorted(report.items, key=_priority_sort_key)[:4]:
        terms.extend([item.module, item.name, item.metric])
    return " ".join(term for term in terms if term)


def _load_formal_run(store: RunStore, formal_run_id: str | None) -> VerificationRun | None:
    if not formal_run_id:
        return None
    try:
        return store.load_run(formal_run_id)
    except FileNotFoundError as exc:
        raise ValueError(f"formal run does not exist: {formal_run_id}") from exc


def _priority_sort_key(item: CoverageItem) -> tuple[int, int, str, str]:
    gap = max(item.goal - item.hits, 0)
    metric_weight = {"functional": 30, "assertion": 24, "code": 20, "fsm": 22}.get(item.metric.lower(), 18)
    return (-(metric_weight + (gap * 10)), item.hits, item.module, item.name)


def _priority_for_item(item: CoverageItem, classification: str) -> int:
    gap = max(item.goal - item.hits, 0)
    metric_weight = {"functional": 65, "assertion": 58, "code": 52, "fsm": 56}.get(item.metric.lower(), 48)
    class_weight = {
        "missing_stimulus": 18,
        "missing_checker": 14,
        "environment_issue": 10,
        "dead_or_unreachable": 6,
        "unknown": 4,
    }.get(classification, 4)
    return metric_weight + (gap * 12) + class_weight


def _reachability_for_item(
    item: CoverageItem,
    hint: CoverageReachabilityHint | None,
    formal_run: VerificationRun | None,
) -> tuple[str, list[str]]:
    if hint is not None:
        return hint.status.lower(), [formal_run.run_id] if formal_run is not None and hint.source == "formal" else []
    if formal_run is None:
        return "unknown", []
    haystacks = [
        formal_run.summary.lower(),
        str((formal_run.tool_result or {}).get("status", "")).lower(),
        " ".join(str(value).lower() for value in (formal_run.tool_result or {}).get("property_ids", [])),
        " ".join(str(value).lower() for value in (formal_run.tool_result or {}).get("report_paths", [])),
    ]
    target_tokens = {token.lower() for token in [item.item_id, item.module, item.name] if token}
    if any("unreach" in haystack for haystack in haystacks) and any(token in " ".join(haystacks) for token in target_tokens):
        return "unreachable", [formal_run.run_id]
    return "unknown", []


def _classify_item(item: CoverageItem, reachability_status: str) -> str:
    if reachability_status == "unreachable":
        return "dead_or_unreachable"
    combined = f"{item.metric} {item.name} {item.detail}".lower()
    if any(term in combined for term in ("assert", "checker", "scoreboard", "coverpoint", "property")):
        return "missing_checker"
    if any(term in combined for term in ("env", "environment", "clock", "reset", "timeout", "setup")):
        return "environment_issue"
    if item.metric.lower() in {"functional", "fsm", "code"}:
        return "missing_stimulus"
    return "unknown"


def _suggested_action(item: CoverageItem, classification: str) -> str:
    target = f"{item.module}::{item.name}"
    if classification == "missing_checker":
        return f"Add a checker or assertion covering {target}."
    if classification == "environment_issue":
        return f"Review reset, clock, or environment setup affecting {target}."
    if classification == "dead_or_unreachable":
        return f"Review {target} as an exclusion candidate and confirm unreachable behavior."
    if classification == "missing_stimulus":
        return f"Add directed cocotb stimulus that exercises {target}."
    return f"Inspect targeted RTL and prior runs for {target} before choosing the next closure action."


def _rationale(
    item: CoverageItem,
    classification: str,
    reachability_status: str,
    supporting_run_ids: list[str],
) -> str:
    gap = max(item.goal - item.hits, 0)
    if classification == "dead_or_unreachable":
        evidence = f" Formal evidence from {', '.join(supporting_run_ids)} supports this classification." if supporting_run_ids else ""
        return f"{item.name} remains uncovered with a gap of {gap}, and reachability is marked {reachability_status}.{evidence}".strip()
    if classification == "missing_checker":
        return f"{item.name} looks checker-oriented and remains uncovered with a gap of {gap}."
    if classification == "environment_issue":
        return f"{item.name} includes environment-like signals or setup indicators and remains uncovered with a gap of {gap}."
    if classification == "missing_stimulus":
        return f"{item.name} appears stimulus-driven and remains uncovered with a gap of {gap}."
    return f"{item.name} remains uncovered with a gap of {gap}, but the current evidence is weak."


def _confidence(
    classification: str,
    evidence_hits: list[RetrievalHit],
    reachability_status: str,
    supporting_run_ids: list[str],
) -> float:
    score = 0.5
    if evidence_hits:
        score += min(len(evidence_hits), 3) * 0.08
    if classification in {"missing_stimulus", "missing_checker"}:
        score += 0.08
    if reachability_status == "unreachable":
        score += 0.12
    if supporting_run_ids:
        score += 0.08
    return round(min(score, 0.95), 2)


def _select_evidence_hits(item: CoverageItem, hits: list[RetrievalHit]) -> list[RetrievalHit]:
    target = f"{item.module} {item.name} {item.metric}".lower()
    target_tokens = set(target.replace("::", " ").split())
    scored: list[tuple[int, RetrievalHit]] = []
    for hit in hits:
        haystack = f"{hit.path} {hit.snippet}".lower()
        overlap = sum(1 for token in target_tokens if token and token in haystack)
        if overlap == 0:
            continue
        scored.append((overlap, hit))
    scored.sort(key=lambda item: (-item[0], -item[1].score, item[1].path))
    selected = [hit for _, hit in scored[:3]]
    return selected or hits[:2]


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"coverage report field `{key}` must be a non-empty string")
    return value


def _parse_item(payload: Any) -> CoverageItem:
    if not isinstance(payload, dict):
        raise ValueError("coverage report items must be objects")
    hits = _coerce_int(payload.get("hits", 0), "item.hits")
    goal = max(_coerce_int(payload.get("goal", 1), "item.goal"), 1)
    coverage = payload.get("coverage")
    if coverage is None:
        coverage_value = round(hits / goal, 3) if goal else 0.0
    else:
        try:
            coverage_value = float(coverage)
        except (TypeError, ValueError) as exc:
            raise ValueError("coverage report item coverage must be numeric") from exc
    return CoverageItem(
        item_id=_required_str(payload, "item_id"),
        module=_required_str(payload, "module"),
        metric=_required_str(payload, "metric"),
        name=_required_str(payload, "name"),
        hits=hits,
        goal=goal,
        coverage=coverage_value,
        detail=str(payload.get("detail", "")).strip(),
    )


def _parse_exclusion(payload: Any) -> CoverageExclusion:
    if not isinstance(payload, dict):
        raise ValueError("coverage exclusions must be objects")
    return CoverageExclusion(
        item_id=_required_str(payload, "item_id"),
        reason=_required_str(payload, "reason"),
        status=str(payload.get("status", "active")).strip() or "active",
    )


def _parse_reachability_hint(payload: Any) -> CoverageReachabilityHint:
    if not isinstance(payload, dict):
        raise ValueError("coverage reachability hints must be objects")
    return CoverageReachabilityHint(
        item_id=_required_str(payload, "item_id"),
        status=_required_str(payload, "status"),
        source=str(payload.get("source", "")).strip(),
        summary=str(payload.get("summary", "")).strip(),
    )


def _parse_object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    parsed: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} entries must be objects")
        parsed.append(item)
    return parsed


def _parse_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} entries must be non-empty strings")
        parsed.append(item.strip().replace("\\", "/"))
    return parsed


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
