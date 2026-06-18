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

from telchines.adapters.open_tools import IcarusAdapter, VerilatorAdapter  # noqa: E402


ADAPTERS = {
    "verilator": VerilatorAdapter,
    "iverilog": IcarusAdapter,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real-tool Telchines adapter smoke tests.")
    parser.add_argument("--adapters", nargs="+", default=["verilator", "iverilog"], choices=sorted(ADAPTERS))
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
            if adapter_name == "iverilog":
                files.append("rtl/smoke_counter_tb.sv")
            execution = adapter.run(f"smoke_{adapter_name}", root, files, artifacts_dir)
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
