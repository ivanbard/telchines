# Generated Artifacts

Telchines generation commands write reviewable drafts under the configured project artifact directory:

```bash
tel gen-sva --spec docs/uart.md --rtl rtl/uart_rx.sv
tel gen-cocotb --dut rtl/uart_rx.sv --spec docs/uart.md
```

The generated files are drafts. The JSON output includes `validation_status`, `validation_mode`, `validation_summary`, `validation_limitations`, and real-tool fields such as `executable_status`, `simulator`, `formal_status`, `formal_adapter`, `command_artifacts`, and `setup_diagnostics` when applicable. Human shell views surface these limitations under `did not prove` so a syntax/structure pass is not mistaken for behavioral signoff.

The built-in heuristic provider can generate a conservative SVA starter without a configured model. It infers only documented clock/reset conventions and DUT ports, includes its evidence citations, and is deliberately limited to a reviewable first pass. Configure a model-backed provider when the design needs richer temporal or protocol-specific assertions.

### SVA Status Contract

`gen-sva` reports independent status fields so a green artifact gate cannot be confused with a proof result:

- `structural_status`: built-in generated-artifact checks, including assertion/property shape and bind references.
- `syntax_status`: parser/lint acceptance when an adapter ran; `not_run` means Telchines did not run a SystemVerilog parser.
- `adapter_status`: configured parser/lint adapter result; `not_run` or `skipped` means no adapter result is available.
- `formal_status`: bounded formal adapter result, or `not_run`/`skipped` when formal execution did not occur.
- `proof_status`: `not_proved` for the current bounded BMC smoke even when `formal_status` is `passed`; this workflow never claims a complete protocol proof.
- `validation_status`: the required artifact-validation gate used for generation retries (`passed` or `failed`).
- `overall_status`: the combined validation outcome. `passed_with_warnings` means required artifact validation passed but optional formal validation failed.
- `workflow_status`: CLI workflow outcome. `validated_with_warnings` preserves a reviewable artifact while making an optional formal failure visible; `candidate_status` remains the narrower artifact-candidate state.

`formal.mode=required` makes a formal setup or execution failure fail the required artifact gate. In `auto` mode, an unavailable formal tool is reported as `skipped`; an attempted formal failure is never silently represented as an ordinary overall pass.

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

When `generation.sva.formal.mode` is `auto` or `required`, Telchines also attempts the configured formal adapter after structural/parser validation succeeds. The default formal adapter is `symbiyosys`. A successful bounded run is reported as `formal_run` with `formal_status: passed` and `proof_status: not_proved`; missing tools are skipped in `auto` mode and fail validation in `required` mode.
When SymbiYosys is missing, `setup_diagnostics` includes `sby` setup hints, including OSS CAD Suite as the practical cross-platform route.

Neither structural nor parser-backed validation proves assertion semantics, vacuity, timing intent, or protocol correctness. Use formal/simulation flows for protocol confidence.

`gen-cocotb` always starts with `syntax_plus_structure` validation. It runs `py_compile` and checks for a cocotb import plus at least one `@cocotb.test` decorator. When `generation.cocotb.executable_smoke` is `auto` or `required`, Telchines probes the selected cocotb/simulator pairing and, when supported, runs a cocotb Makefile smoke with Icarus or Verilator. A passing smoke upgrades `validation_mode` to `compile_and_run`; unavailable or unsupported pairings are explicitly skipped in `auto` mode and fail validation in `required` mode.

Executable smoke metadata includes the Makefile, compile/run logs, command argv, setup diagnostics, and a bounded `environment_summary` that redacts secret-looking run-spec keys and summarizes inherited `PYTHONPATH` entries. The manifest and JSON response expose `validation_stages` for `python_syntax`, `structural_validation`, `simulator_compile`, `simulator_launch`, `cocotb_init`, and `test_results`, so an embedded-Python or simulator failure is never reported as a Python syntax error. Each simulator phase has a 120-second bound; on timeout Telchines terminates the complete process tree, including child simulator processes.

For an executable smoke in developer or release environments, install the optional Python dependency and provide `make`, Icarus, and `vvp` on `PATH`:

```bash
python -m pip install -e ".[cocotb-smoke]"
pytest tests/test_cocotb_smoke.py
```

That test generates a cocotb scaffold for a tiny UART fixture and asserts Telchines' own executable smoke path passes. It skips automatically when `cocotb`, cocotb's makefiles, `make`, `iverilog`, `vvp`, or the cocotb VPI binding are unavailable or incompatible. Telchines can discover cocotb makefiles through either `cocotb-config --makefiles` or `python -m cocotb_tools.config --makefiles`, so a missing `cocotb-config` console script is not by itself fatal when the Python package is installed. The MSYS `make` plus native Windows Icarus pairing is explicitly unsupported because cocotb's Makefile runner cannot reliably initialize embedded Python in `vvp`; `auto` mode records a skip, and `required` mode returns a stage-level failure. Use WSL/Linux or a separately validated non-MSYS simulator setup for executable validation on Windows.

For a manual or CI smoke command that does not require remembering the pytest name, use:

```bash
python scripts/tool_smoke.py --adapters iverilog --cocotb
```

Add `--allow-missing` when checking a developer machine where optional tools may be absent; missing cocotb setup is reported as a skip with setup diagnostics.

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
- Read `validation_limitations` before trusting a generated artifact; syntax, structure, parser, adapter, simulator, and formal modes prove different things.
- Keep generated artifacts under version control only after human review.
- Use `tel artifacts review REF` after editing a generated artifact to see what changed from the model/provider draft.
- Inspect the saved request/response artifacts when a draft looks surprising.
- Use `tel artifacts purge` to inspect or clean stored generated artifacts and provider payloads.

## Retention And Privacy

Generated files, generation candidates, validation runs, patches, reports, waveform summaries, replay metadata, and task artifacts live under `.tel/`. Task artifacts intentionally keep prompts, retrieved RTL/spec/log context, and provider responses for replayability. Telchines redacts dictionary fields with credential-looking keys before saving task artifacts, but it does not redact proprietary design content.

Run `tel doctor privacy` to inspect retention guidance. `tel artifacts purge` is a dry-run preview by default; add `--yes` to delete matching payloads. Use scopes or an age window when you need narrower retention:

```bash
tel artifacts purge
tel artifacts purge --scope task-artifacts --scope reports
tel artifacts purge --older-than-days 30
tel artifacts purge --scope task-artifacts --older-than-days 30 --yes
```

Supported purge scopes are `generated`, `task-artifacts`, `patches`, `generations`, `waveforms`, and `reports`. Purge removes artifact payloads but preserves run records, retrieval contexts, observations, project config, and workspace files.
