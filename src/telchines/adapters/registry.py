from __future__ import annotations

from telchines.adapters.base import ToolAdapter
from telchines.adapters.open_tools import IcarusAdapter, SymbiYosysAdapter, VeribleAdapter, VerilatorAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ToolAdapter] = {
            "verilator": VerilatorAdapter(),
            "iverilog": IcarusAdapter(),
            "verible": VeribleAdapter(),
            "symbiyosys": SymbiYosysAdapter(),
        }

    def get(self, name: str) -> ToolAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown adapter: {name}") from exc

    def list_adapters(self) -> list[ToolAdapter]:
        return list(self._adapters.values())
