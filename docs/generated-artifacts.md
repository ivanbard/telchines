# Generated Artifacts

Telchines generation commands write reviewable drafts under the configured project artifact directory:

```bash
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md
```

The generated files are drafts. The JSON output includes `validation_status`, `validation_mode`, `validation_summary`, and `validation_limitations` so reviewers can see what was checked and what still needs tool-backed review.

## Validation Modes

`gen-sva` always starts with built-in structural validation. It checks module/property block balance, presence of `assert property`, bind target module names, checker module definitions, and obvious DUT signal references in bind connections.

If the built-in checks pass, Telchines tries the configured `generation.sva.validation_adapters` in order. The default order is `slang`, then `verilator`. The first enabled and available adapter that supports `generation_validation` is run against the DUT RTL plus generated assertion artifact, and the result is reported as `adapter_backed`. If no configured adapter can run, Telchines falls back to `builtin_structural` and includes adapter fallback reasons in the validation run's `tool_result.checks`.

Neither mode proves assertion semantics, vacuity, timing intent, or protocol correctness. Use formal/simulation flows for protocol confidence.

`gen-cocotb` uses `python_syntax_plus_structure` validation by default. It runs `py_compile` and checks for a cocotb import plus at least one `@cocotb.test` decorator. It does not run a simulator. Executable cocotb validation still requires optional cocotb and simulator tooling.

## Project Conventions

Projects can configure generation naming and inference conventions in `.tel/config.json`:

```json
{
  "generation": {
    "sva": {
      "output_dir": ".tel/artifacts/generated",
      "filename_template": "{module}_assertions.sv",
      "clock_names": ["clk", "clock"],
      "reset_names": ["rst_n", "reset_n", "rst", "reset"],
      "active_low_reset_names": ["rst_n", "reset_n"],
      "validation_adapters": ["slang", "verilator"]
    },
    "cocotb": {
      "output_dir": ".tel/artifacts/generated/cocotb",
      "test_file_template": "test_{module}.py",
      "manifest_file_template": "{module}_cocotb_manifest.json",
      "clock_names": ["clk", "clock"],
      "reset_names": ["rst_n", "reset_n", "rst", "reset"],
      "active_low_reset_names": ["rst_n", "reset_n"]
    }
  }
}
```

Templates support `{module}`, `{rtl_stem}`, and `{dut_stem}`. Template values must be file-name templates, not paths; use `output_dir` for directories.

## Review Notes

- Treat generated SVA and cocotb as reviewable starting points, not accepted verification IP.
- Keep generated artifacts under version control only after human review.
- Inspect the saved request/response artifacts when a draft looks surprising.
- Use `tel artifacts purge` to inspect or clean stored generated artifacts and provider payloads.
