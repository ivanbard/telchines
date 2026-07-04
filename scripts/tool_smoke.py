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
            missing = [binary for binary in adapter.required_binaries or adapter.binary_names if shutil.which(binary) is None]
            if missing:
                message = f"{adapter_name}: missing required binaries: {', '.join(missing)}"
                if args.allow_missing:
                    print(f"SKIP {message}")
                    continue
                print(f"FAIL {message}")
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
smtbmc

[script]
read -formal rtl/smoke_counter.sv
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

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
