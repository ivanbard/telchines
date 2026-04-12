from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from telchines.adapters.base import ToolAdapter


class VerilatorAdapter(ToolAdapter):
    name = "verilator"
    kind = "simulator"
    category = "simulation"
    binary_names = ("verilator",)
    supported_workflows = ("repair_validation",)
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["verilator", "--lint-only", *(extra_args or []), *files]


class IcarusAdapter(ToolAdapter):
    name = "iverilog"
    kind = "simulator"
    category = "simulation"
    binary_names = ("iverilog",)
    supported_workflows = ("repair_validation",)
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["iverilog", "-g2012", *(extra_args or []), *files]


class SlangAdapter(ToolAdapter):
    name = "slang"
    kind = "simulator"
    category = "simulation"
    binary_names = ("slang",)
    supported_workflows = ("repair_validation", "generation_validation")
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["slang", "--lint-only", *(extra_args or []), *files]


class VeribleAdapter(ToolAdapter):
    name = "verible"
    kind = "linter"
    category = "lint"
    binary_names = ("verible-verilog-lint",)
    supported_workflows = ("repair_validation",)
    artifact_types = ("log", "stdout", "stderr")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["verible-verilog-lint", *(extra_args or []), *files]


class SymbiYosysAdapter(ToolAdapter):
    name = "symbiyosys"
    kind = "formal"
    category = "formal"
    binary_names = ("sby",)
    supported_workflows = ("formal_validation",)
    artifact_types = ("log", "report", "counterexample")

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["sby", *(extra_args or []), *files]

    def parse_result(self, project_root: Path, files: list[str], stdout: str, stderr: str, combined: str) -> dict[str, Any]:
        return _parse_symbiyosys_result(project_root, combined)


def _parse_symbiyosys_result(project_root: Path, combined: str) -> dict[str, Any]:
    lowered = combined.lower()
    status = "unknown"
    if re.search(r"\b(status|summary)\s*:\s*passed\b", lowered):
        status = "passed"
    elif re.search(r"\b(status|summary)\s*:\s*failed\b", lowered):
        status = "failed"
    elif "assert failed" in lowered or "counterexample" in lowered:
        status = "failed"

    property_ids = sorted(
        {
            match.group("name")
            for match in re.finditer(
                r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s+(?:proved|failed|covered|unreached)",
                combined,
                flags=re.IGNORECASE,
            )
        }
    )
    referenced_artifacts = _existing_artifacts(project_root, combined)
    counterexample_paths = [path for path in referenced_artifacts if path.endswith((".vcd", ".fst"))]
    report_paths = [path for path in referenced_artifacts if path.endswith((".json", ".txt", ".log"))]
    return {
        "status": status,
        "property_ids": property_ids,
        "counterexample_paths": counterexample_paths,
        "report_paths": report_paths,
        "referenced_artifacts": referenced_artifacts,
    }


def _existing_artifacts(project_root: Path, combined: str) -> list[str]:
    candidates = re.findall(r"([A-Za-z0-9_./\\\\-]+\.(?:vcd|fst|json|txt|log))", combined)
    artifacts: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = Path(candidate)
        resolved = path if path.is_absolute() else (project_root / path)
        try:
            normalized = str(resolved.resolve().relative_to(project_root.resolve())).replace("\\", "/")
        except ValueError:
            normalized = str(resolved.resolve()).replace("\\", "/")
        if not resolved.exists() or normalized in seen:
            continue
        seen.add(normalized)
        artifacts.append(normalized)
    return artifacts
