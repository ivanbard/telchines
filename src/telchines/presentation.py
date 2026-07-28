from __future__ import annotations

import io
import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def _render_rich(renderable: Any, width: int = 100) -> str:
    output = io.StringIO()
    console = Console(file=output, width=width, force_terminal=False, color_system=None, legacy_windows=True)
    console.print(renderable)
    return output.getvalue().rstrip()


def render_action_panel(title: str, body: str) -> str:
    return _render_rich(Panel(body, title=title, border_style="cyan"))


def render_get_started(payload: dict[str, object]) -> str:
    inputs = payload["inputs"]
    lines = [
        f"directory: {payload['root']}",
        f"project: {'detected' if payload['project_detected'] else 'not initialized'}",
    ]
    for label in ("rtl", "docs", "logs", "coverage"):
        paths = inputs.get(label, []) if isinstance(inputs, dict) else []
        lines.append(f"{label}: {len(paths)}" + (f" ({paths[0]})" if paths else ""))
    if payload.get("initialized"):
        lines.append("project: initialized")
        lines.append(f"indexed chunks: {payload['indexed_chunks']}")
    recommendation = payload["recommendation"]
    if isinstance(recommendation, dict):
        lines.extend(["", f"next: {recommendation['command']}", f"why: {recommendation['reason']}"])
    return render_action_panel("Get Started", "\n".join(lines))


def render_project_init(root: str, project_id: str, template: str | None = None) -> str:
    lines = [f"root: {root}", f"project: {project_id}"]
    if template:
        lines.append(f"template: {template}")
    lines.extend(["index: not built", "next: tel index"])
    return render_action_panel("Project Initialized", "\n".join(lines))


def render_project_templates(payload: dict[str, object]) -> str:
    table = Table(title="Project Templates", show_header=True, header_style="bold cyan")
    table.add_column("Template")
    table.add_column("Description")
    for item in payload.get("templates", []):
        if isinstance(item, dict):
            table.add_row(str(item.get("name", "")), str(item.get("description", "")))
    return _render_rich(table)


def render_index_status_payload(payload: dict[str, object]) -> str:
    table = Table(title="Index Status", show_header=True, header_style="bold cyan")
    table.add_column("Index")
    table.add_column("State")
    table.add_column("Chunks")
    table.add_column("Sources")
    table.add_column("Missing/Stale/Deleted")
    for label in ("project", "external"):
        item = payload[label]
        table.add_row(label, "stale" if item["stale"] else "fresh", str(item["chunk_count"]), str(item["source_count"]), f"{item['missing_source_count']}/{item['stale_source_count']}/{item['deleted_source_count']}")
    return _render_rich(table)


def render_retrieval_payload(payload: dict[str, object]) -> str:
    table = Table(title=f"Retrieval Hits {payload['context_id']}", show_header=True, header_style="bold cyan")
    table.add_column("Citation")
    table.add_column("Kind")
    table.add_column("Score")
    table.add_column("Snippet")
    for hit in payload["hits"]:
        snippet = " ".join(str(hit["snippet"]).splitlines()[:2]).strip()
        table.add_row(hit["citation"], hit["kind"], str(hit["score"]), snippet)
    return _render_rich(table)


def render_provider_payload(payload: dict[str, object]) -> str:
    defaults = payload.get("default_provider_by_capability", {})
    table = Table(title="Provider Status", show_header=True, header_style="bold cyan")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Capabilities")
    table.add_column("Status")
    for provider in payload["providers"]:
        status = "allowed" if provider["allowed"] else f"blocked: {provider['blocked_reason']}"
        table.add_row(provider["name"], provider["kind"], ", ".join(provider["capabilities"]), status)
    default_lines = [f"{capability}: {provider}" for capability, provider in dict(defaults).items()] if isinstance(defaults, dict) else []
    return "Default Providers\n" + ("\n".join(default_lines) or "none configured") + "\n\n" + _render_rich(table)


def render_adapters_payload(payload: dict[str, object]) -> str:
    table = Table(title="Adapters", show_header=True, header_style="bold cyan")
    table.add_column("Adapter")
    table.add_column("Category")
    table.add_column("Available")
    for adapter in payload.get("adapters", []):
        if isinstance(adapter, dict):
            table.add_row(str(adapter.get("name", "")), str(adapter.get("category", adapter.get("kind", ""))), "yes" if adapter.get("available") else "no")
    return _render_rich(table)


def render_runs_payload(payload: list[dict[str, object]]) -> str:
    if not payload:
        return render_action_panel("Runs", "no runs recorded")
    table = Table(title="Recent Runs", show_header=True, header_style="bold cyan")
    table.add_column("Run ID")
    table.add_column("Workflow")
    table.add_column("Status")
    table.add_column("Tool")
    for run in payload[:10]:
        table.add_row(run["run_id"], run["workflow_type"], run["status"], run["tool"]["name"])
    return _render_rich(table)


def render_run_show(payload: dict[str, object]) -> str:
    lines = [f"run: {payload['run_id']}", f"workflow: {payload['workflow_type']}", f"status: {payload['status']}", f"tool: {payload['tool']['name']}", f"summary: {payload['summary']}"]
    return render_action_panel("Run Detail", "\n".join(lines))


def render_doctor_summary(payload: dict[str, object]) -> str:
    if payload.get("status") == "not_initialized":
        return render_action_panel("Project Health", f"project: not initialized\nnext: {payload['next_action']}")
    project = payload["project"]
    index = payload["index"]
    adapters = payload["adapters"]
    lines = [
        f"status: {payload['status']}",
        f"project: {project['name']}",
        f"root: {project['root']}",
        f"index: {'stale' if index['stale'] else 'fresh'} ({index['chunk_count']} chunks)",
        f"providers: {payload['providers']['status']}",
        f"adapters: {adapters['available']}/{adapters['total']} available",
        f"artifacts: {payload['artifacts_dir']}",
        f"next: {payload['next_action']}",
    ]
    return render_action_panel("Project Health", "\n".join(lines))


def render_recipe_result(title: str, payload: dict[str, object], next_action: str) -> str:
    lines = [f"status: {payload.get('status', 'completed')}"]
    for key in ("run_id", "artifact_path", "validation_status", "provider", "cluster_count"):
        value = payload.get(key)
        if value is not None and value != "":
            lines.append(f"{key.replace('_', ' ')}: {value}")
    lines.append(f"next: {next_action}")
    return render_action_panel(title, "\n".join(lines))


def render_payload(title: str, payload: object) -> str:
    """Render an inspection payload consistently for one-shot CLI output."""
    return render_action_panel(title, json.dumps(payload, indent=2, sort_keys=True, default=str))
