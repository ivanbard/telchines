from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.errors import ProjectNotInitializedError
from telchines.operations import index_project, initialize_project


RTL_SUFFIXES = {".sv", ".svh", ".v", ".vh"}
DOC_SUFFIXES = {".md", ".rst"}
LOG_SUFFIXES = {".log", ".out"}
IGNORED_DIRECTORIES = {
    ".git",
    ".tel",
    ".venv",
    ".test-work",
    ".pytest_tmp",
    ".hypothesis",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
}


def inspect_get_started(root: Path) -> dict[str, object]:
    """Inspect a directory without creating project state or indexes."""
    root = root.resolve()
    project_config = _discover_project(root)
    inputs = _detect_inputs(root)
    recommendation = _recommend(inputs, project_config is not None)
    return {
        "root": str(root),
        "project_detected": project_config is not None,
        "project_root": str(project_config.project_root) if project_config else None,
        "inputs": inputs,
        "recommendation": recommendation,
    }


def initialize_and_index_get_started(root: Path) -> dict[str, object]:
    """Create a default project, index it, then return the guided result."""
    root = root.resolve()
    project_config = _discover_project(root)
    initialized = project_config is None
    if project_config is None:
        project_config = initialize_project(root)
    indexed_chunks = index_project(project_config.project_root)
    payload = inspect_get_started(project_config.project_root)
    payload["initialized"] = initialized
    payload["indexed_chunks"] = indexed_chunks
    return payload


def _discover_project(root: Path) -> ProjectConfig | None:
    try:
        return ProjectConfig.discover(root)
    except ProjectNotInitializedError:
        return None


def _detect_inputs(root: Path) -> dict[str, list[str]]:
    discovered = {"rtl": [], "docs": [], "logs": [], "coverage": []}
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in relative_parts):
            continue
        if relative_parts and relative_parts[0] == "examples":
            continue
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in RTL_SUFFIXES:
            discovered["rtl"].append(relative)
        if suffix in DOC_SUFFIXES:
            discovered["docs"].append(relative)
        if suffix in LOG_SUFFIXES:
            discovered["logs"].append(relative)
        if _is_coverage_path(relative):
            discovered["coverage"].append(relative)
    return {kind: sorted(paths) for kind, paths in discovered.items()}


def _is_coverage_path(relative: str) -> bool:
    lowered = relative.lower()
    return "/cov/" in f"/{lowered}" or "coverage" in Path(lowered).name


def _recommend(inputs: dict[str, list[str]], project_detected: bool) -> dict[str, str]:
    if not project_detected:
        return {
            "kind": "initialize",
            "command": "tel get-started --init",
            "reason": "Initialize this directory and build its first retrieval index.",
        }
    if inputs["logs"]:
        return {
            "kind": "triage",
            "command": f"tel triage --logs {inputs['logs'][0]} --format human",
            "reason": "Regression logs are available for investigation.",
        }
    if inputs["coverage"]:
        return {
            "kind": "coverage",
            "command": f"tel coverage-plan --report {inputs['coverage'][0]} --format human",
            "reason": "A coverage report is available for closure planning.",
        }
    if inputs["rtl"] and inputs["docs"]:
        return {
            "kind": "sva",
            "command": f"tel gen-sva --spec {inputs['docs'][0]} --rtl {inputs['rtl'][0]}",
            "reason": "RTL and specification context are available for assertion generation.",
        }
    if inputs["rtl"]:
        return {
            "kind": "retrieve",
            "command": 'tel retrieve "describe the RTL design" --format human',
            "reason": "RTL is available; start by exploring the indexed design context.",
        }
    return {
        "kind": "add-inputs",
        "command": "Add RTL, documentation, regression logs, or a coverage report, then run tel get-started.",
        "reason": "No supported verification inputs were detected.",
    }
