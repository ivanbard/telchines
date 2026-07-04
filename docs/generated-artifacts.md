# Generated Artifacts

Telchines generation commands write reviewable drafts under the configured project artifact directory:

```bash
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md
```

The generated files are drafts. The JSON output includes `validation_status`, `validation_mode`, `validation_summary`, `validation_limitations`, and real-tool fields such as `executable_status`, `simulator`, `formal_status`, `formal_adapter`, `command_artifacts`, and `setup_diagnostics` when applicable.

If `generation.sva.max_attempts` or `generation.cocotb.max_attempts` is greater than `1`, failed validation output is fed back into the next model request. JSON output then includes `attempts` and `rejected_candidate_ids`, and each rejected candidate remains reviewable by id.

Use `tel artifacts review REF` to compare a saved generated candidate with the current workspace file. `REF` can be a generation candidate id, a validation run id, or the generated artifact path:

```bash
tel artifacts review cocotb_b33cb78ea546
tel artifacts review run_b4da02ba2260 --max-diff-lines 80
```

The command reports whether the artifact is unchanged, modified, or missing and includes a bounded unified diff for human review.

## Validation Modes

`gen-sva` always starts with built-in structural validation. It checks module/property block balance, presence of `assert property`, bind target module names, checker module definitions, and obvious DUT signal references in bind connections.

If the built-in checks pass, Telchines tries the configured `generation.sva.validation_adapters` in order. The default order is `slang`, then `verilator`. The first enabled and available adapter that supports `generation_validation` is run against the DUT RTL plus generated assertion artifact, and the result is reported as `adapter_backed`. If no configured adapter can run, Telchines falls back to `structure_only` and includes adapter fallback reasons in the validation run's `tool_result.checks`.

When `generation.sva.formal.mode` is `auto` or `required`, Telchines also attempts the configured formal adapter after structural/parser validation succeeds. The default formal adapter is `symbiyosys`. A successful bounded run is reported as `formal_run`; missing tools are skipped in `auto` mode and fail validation in `required` mode.

Neither structural nor parser-backed validation proves assertion semantics, vacuity, timing intent, or protocol correctness. Use formal/simulation flows for protocol confidence.

`gen-cocotb` always starts with `syntax_plus_structure` validation. It runs `py_compile` and checks for a cocotb import plus at least one `@cocotb.test` decorator. When `generation.cocotb.executable_smoke` is `auto` or `required`, Telchines attempts a cocotb Makefile smoke with Icarus or Verilator when the required tools are available. A passing smoke upgrades `validation_mode` to `compile_and_run`; missing tools are skipped in `auto` mode and fail validation in `required` mode.

For an executable smoke in developer or release environments, install the optional Python dependency and provide Icarus plus cocotb's makefile tooling on `PATH`:

```bash
python -m pip install -e ".[cocotb-smoke]"
pytest tests/test_cocotb_smoke.py
```

That test generates a cocotb scaffold for a tiny UART fixture and asserts Telchines' own executable smoke path passes. It skips automatically when `cocotb`, `cocotb-config`, `make`, `iverilog`, or `vvp` are unavailable.

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
      "validation_adapters": ["slang", "verilator"],
      "formal": {"mode": "auto", "adapter": "symbiyosys"},
      "max_attempts": 1
    },
    "cocotb": {
      "output_dir": ".tel/artifacts/generated/cocotb",
      "test_file_template": "test_{module}.py",
      "manifest_file_template": "{module}_cocotb_manifest.json",
      "clock_names": ["clk", "clock"],
      "reset_names": ["rst_n", "reset_n", "rst", "reset"],
      "active_low_reset_names": ["rst_n", "reset_n"],
      "executable_smoke": "auto",
      "simulator": "auto",
      "max_attempts": 1
    }
  }
}
```

Templates support `{module}`, `{rtl_stem}`, and `{dut_stem}`. Template values must be file-name templates, not paths; use `output_dir` for directories.

## Review Notes

- Treat generated SVA and cocotb as reviewable starting points, not accepted verification IP.
- Keep generated artifacts under version control only after human review.
- Use `tel artifacts review REF` after editing a generated artifact to see what changed from the model/provider draft.
- Inspect the saved request/response artifacts when a draft looks surprising.
- Use `tel artifacts purge` to inspect or clean stored generated artifacts and provider payloads.
