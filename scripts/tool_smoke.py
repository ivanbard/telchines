from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.exists():
    sys.path.insert(0, str(SRC_ROOT))

from telchines.adapters.base import AdapterRunSpec  # noqa: E402
from telchines.adapters.open_tools import IcarusAdapter, SlangAdapter, SymbiYosysAdapter, VerilatorAdapter  # noqa: E402
from telchines.config import ProjectConfig  # noqa: E402
from telchines.models import CocotbCandidate  # noqa: E402
from telchines.run_store import RunStore  # noqa: E402
from telchines.workflows.gen_cocotb import _cocotb_common_missing, _cocotb_makefiles_dir, _select_cocotb_simulator, validate_cocotb_candidate  # noqa: E402


ADAPTERS = {
    "verilator": VerilatorAdapter,
    "iverilog": IcarusAdapter,
    "slang": SlangAdapter,
    "symbiyosys": SymbiYosysAdapter,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real-tool Telchines adapter smoke tests.")
    parser.add_argument("--adapters", nargs="+", default=["verilator", "iverilog", "slang", "symbiyosys"], choices=sorted(ADAPTERS))
    parser.add_argument("--allow-missing", action="store_true", help="Skip adapters whose required binaries are not on PATH.")
    parser.add_argument("--cocotb", action="store_true", help="Also run Telchines' executable cocotb smoke with Icarus when cocotb tooling is installed.")
    args = parser.parse_args()

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="telchines-tool-smoke-") as tmp:
        root = Path(tmp)
        rtl_dir = root / "rtl"
        artifacts_dir = root / "artifacts"
        rtl_dir.mkdir(parents=True)
        artifacts_dir.mkdir(parents=True)
        lint_file = rtl_dir / "smoke_counter.sv"
        lint_file.write_text(
            """module smoke_counter(
  input logic clk,
  input logic rst_n,
  output logic [3:0] count
);

always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) count <= 4'd0;
  else count <= count + 4'd1;
end

endmodule
""",
            encoding="utf-8",
        )
        tb_file = rtl_dir / "smoke_counter_tb.sv"
        tb_file.write_text(
            """module smoke_counter_tb;
  logic clk = 0;
  logic rst_n = 0;
  logic [3:0] count;

  smoke_counter dut(.clk(clk), .rst_n(rst_n), .count(count));
  always #1 clk = ~clk;

  initial begin
    #2 rst_n = 1;
    #10 $display("count=%0d", count);
    $finish;
  end
endmodule
""",
            encoding="utf-8",
        )
        include_dir = root / "rtl" / "include"
        include_dir.mkdir()
        (include_dir / "smoke_defs.svh").write_text("`define TELCHINES_SMOKE 1\n", encoding="utf-8")
        filelist = root / "smoke_files.f"
        filelist.write_text(
            "\n".join(
                [
                    "# Telchines real-tool smoke filelist",
                    "+incdir+rtl/include",
                    "+define+TELCHINES_TOOL_SMOKE=1",
                    "rtl/smoke_counter.sv",
                    "rtl/smoke_counter_tb.sv",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        for adapter_name in args.adapters:
            adapter = ADAPTERS[adapter_name]()
            if not _adapter_available(adapter):
                missing = _adapter_missing_binaries(adapter)
                message = f"{adapter_name}: missing required binaries: {', '.join(missing)}"
                if args.allow_missing:
                    print(f"SKIP {message}")
                    for diagnostic in _adapter_setup_diagnostics(adapter, missing):
                        print(f"  {diagnostic}")
                    continue
                print(f"FAIL {message}")
                for diagnostic in _adapter_setup_diagnostics(adapter, missing):
                    print(f"  {diagnostic}")
                failures.append(message)
                continue

            files = ["rtl/smoke_counter.sv"]
            spec = AdapterRunSpec(files=files, include_dirs=["rtl/include"], defines=["TELCHINES_TOOL_SMOKE=1"], top_module="smoke_counter")
            if adapter_name == "iverilog":
                files = ["rtl/smoke_counter.sv", "rtl/smoke_counter_tb.sv"]
                spec = AdapterRunSpec(filelists=[filelist.name], top_module="smoke_counter_tb")
            if adapter_name == "symbiyosys":
                sby_file = root / "smoke_counter.sby"
                sby_file.write_text(
                    """[options]
mode bmc
depth 2

[engines]
smtbmc z3

[script]
read -formal smoke_counter.sv
prep -top smoke_counter

[files]
rtl/smoke_counter.sv
""",
                    encoding="utf-8",
                )
                files = [sby_file.name]
                spec = AdapterRunSpec(files=files)
            execution = adapter.run(f"smoke_{adapter_name}", root, files, artifacts_dir, spec=spec)
            status = "PASS" if execution.exit_code == 0 else "FAIL"
            print(f"{status} {adapter_name}: {execution.summary}")
            if execution.exit_code != 0:
                failures.append(f"{adapter_name}: exit code {execution.exit_code}")

        if args.cocotb:
            _run_cocotb_smoke(root, failures, allow_missing=args.allow_missing)

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


def _run_cocotb_smoke(root: Path, failures: list[str], *, allow_missing: bool) -> None:
    simulator, simulator_diagnostics = _select_cocotb_simulator("icarus")
    _, makefile_diagnostics = _cocotb_makefiles_dir()
    diagnostics = [*simulator_diagnostics, *_cocotb_common_missing(), *makefile_diagnostics]
    if diagnostics:
        message = f"cocotb: {'; '.join(diagnostics)}"
        if allow_missing:
            print(f"SKIP {message}")
            return
        print(f"FAIL {message}")
        failures.append(message)
        return

    config = ProjectConfig.init_project(root)
    config.generation["cocotb"]["executable_smoke"] = "required"
    config.generation["cocotb"]["simulator"] = simulator or "icarus"
    config.save()
    candidate = CocotbCandidate(
        candidate_id="cocotb_tool_smoke",
        task_id="task_cocotb_tool_smoke",
        dut_path="rtl/smoke_counter.sv",
        spec_path=None,
        top_module="smoke_counter",
        file_path=".tel/artifacts/generated/cocotb/test_smoke_counter.py",
        manifest_path=".tel/artifacts/generated/cocotb/smoke_counter_cocotb_manifest.json",
        candidate_content="""import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


@cocotb.test()
async def test_smoke_counter(dut):
    cocotb.start_soon(Clock(dut.clk, 2, units="ns").start())
    dut.rst_n.value = 0
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    assert int(dut.count.value) >= 1
""",
        explanation="real cocotb smoke",
        status="proposed",
    )
    validation = validate_cocotb_candidate(
        config,
        RunStore(config),
        candidate,
        run_spec=AdapterRunSpec(files=["rtl/smoke_counter.sv"], top_module="smoke_counter"),
    )
    if validation.tool_result.get("executable_contract") == "unsupported" and allow_missing:
        diagnostics = validation.tool_result.get("setup_diagnostics", [])
        print(f"SKIP cocotb: {'; '.join(str(item) for item in diagnostics)}")
        return
    status = "PASS" if validation.status == "passed" else "FAIL"
    print(f"{status} cocotb: {validation.summary}")
    if validation.status != "passed":
        failures.append(f"cocotb: {validation.summary}")


def _adapter_available(adapter) -> bool:  # noqa: ANN001
    if hasattr(adapter, "is_available"):
        return bool(adapter.is_available())
    if hasattr(adapter, "missing_binaries"):
        return not adapter.missing_binaries()
    required = getattr(adapter, "required_binaries", None) or getattr(adapter, "binary_names", ())
    return all(shutil.which(binary) for binary in required)


def _adapter_missing_binaries(adapter) -> list[str]:  # noqa: ANN001
    if hasattr(adapter, "missing_binaries"):
        return list(adapter.missing_binaries())
    required = getattr(adapter, "required_binaries", None) or getattr(adapter, "binary_names", ())
    return [binary for binary in required if shutil.which(binary) is None]


def _adapter_setup_diagnostics(adapter, missing: list[str]) -> list[str]:  # noqa: ANN001
    if hasattr(adapter, "setup_diagnostics"):
        return list(adapter.setup_diagnostics(missing))
    return [f"install/configure adapter and ensure these binaries are on PATH: {', '.join(missing)}"] if missing else []


if __name__ == "__main__":
    raise SystemExit(main())
