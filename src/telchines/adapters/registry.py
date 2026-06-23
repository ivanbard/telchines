from __future__ import annotations

import sys
from pathlib import Path

from telchines.adapters.base import ToolAdapter
from telchines.adapters.open_tools import IcarusAdapter, SlangAdapter, SymbiYosysAdapter, VeribleAdapter, VerilatorAdapter


class FixtureAdapter(ToolAdapter):
    name = "fixture"
    kind = "linter"
    category = "simulation"
    validation_mode = "fixture_lint"

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return [sys.executable, "tools/fixture_lint.py", *files, *(extra_args or [])]


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ToolAdapter] = {
            "fixture": FixtureAdapter(),
            "verilator": VerilatorAdapter(),
            "iverilog": IcarusAdapter(),
            "slang": SlangAdapter(),
            "verible": VeribleAdapter(),
            "symbiyosys": SymbiYosysAdapter(),
        }

    def get(self, name: str) -> ToolAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown adapter: {name}") from exc

    def list(self, *, category: str | None = None) -> list[ToolAdapter]:
        adapters = list(self._adapters.values())
        if category is not None:
            adapters = [adapter for adapter in adapters if adapter.category == category or adapter.kind == category]
        return sorted(adapters, key=lambda adapter: adapter.name)
