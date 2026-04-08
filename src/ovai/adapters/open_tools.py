from __future__ import annotations

from pathlib import Path

from ovai.adapters.base import ToolAdapter


class VerilatorAdapter(ToolAdapter):
    name = "verilator"
    kind = "simulator"
    binary_names = ("verilator",)

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["verilator", "--lint-only", *(extra_args or []), *files]


class IcarusAdapter(ToolAdapter):
    name = "iverilog"
    kind = "simulator"
    binary_names = ("iverilog",)

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["iverilog", "-g2012", *(extra_args or []), *files]


class VeribleAdapter(ToolAdapter):
    name = "verible"
    kind = "linter"
    binary_names = ("verible-verilog-lint",)

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["verible-verilog-lint", *(extra_args or []), *files]


class SymbiYosysAdapter(ToolAdapter):
    name = "symbiyosys"
    kind = "formal"
    binary_names = ("sby",)

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["sby", *(extra_args or []), *files]
