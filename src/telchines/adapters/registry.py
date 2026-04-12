from __future__ import annotations

from telchines.adapters.base import ToolAdapter
from telchines.adapters.open_tools import IcarusAdapter, SlangAdapter, SymbiYosysAdapter, VeribleAdapter, VerilatorAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ToolAdapter] = {
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
