from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from telchines.adapters.base import AdapterRunSpec, ToolAdapter
from telchines.adapters.open_tools import IcarusAdapter, SlangAdapter, SymbiYosysAdapter, VeribleAdapter, VerilatorAdapter
from telchines.adapters.registry import AdapterRegistry
from telchines.errors import AdapterExecutionError
from telchines.operations import check_adapters


SV_IDENTIFIER = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True).filter(lambda value: value not in {"module", "endmodule"})
PATH_PART = st.from_regex(r"[A-Za-z0-9_]{1,10}", fullmatch=True)
RELATIVE_PATH = st.builds(lambda folder, name: f"{folder}/{name}.sv", PATH_PART, PATH_PART)
INCLUDE_DIR = st.builds(lambda folder: f"rtl/{folder}", PATH_PART)
DEFINE = st.one_of(
    SV_IDENTIFIER,
    st.builds(lambda name, value: f"{name}={value}", SV_IDENTIFIER, st.from_regex(r"[A-Za-z0-9_]{1,8}", fullmatch=True)),
)
ARG = st.from_regex(r"-[A-Za-z0-9_=-]{1,12}", fullmatch=True)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


class EchoAdapter(ToolAdapter):
    name = "echo"
    kind = "tool"
    category = "tool"
    binary_names = ()
    required_binaries = ()

    def build_command(self, project_root: Path, files: list[str], extra_args: list[str] | None = None) -> list[str]:
        return ["echo-adapter", *(extra_args or []), *files]


def test_adapter_registry_lists_categories() -> None:
    registry = AdapterRegistry()
    simulation_names = [adapter.name for adapter in registry.list(category="simulation")]
    assert "verilator" in simulation_names
    assert "iverilog" in simulation_names
    assert "slang" in simulation_names
    assert [adapter.name for adapter in registry.list(category="formal")] == ["symbiyosys"]
    iverilog = next(adapter for adapter in registry.list(category="simulation") if adapter.name == "iverilog")
    descriptor = iverilog.describe(enabled=True)
    assert descriptor.validation_mode == "compile_and_run"
    assert descriptor.required_binaries == ["iverilog", "vvp"]


def test_symbiyosys_adapter_parses_structured_results(work_root) -> None:
    trace_path = work_root / "engine_0" / "trace.vcd"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("$date today $end\n", encoding="utf-8")
    report_path = work_root / "engine_0" / "summary.txt"
    report_path.write_text("formal summary\n", encoding="utf-8")

    combined = """
SBY 12:00:00 [proof] engine_0: status: failed
SBY 12:00:00 [proof] uart_start_seen failed
SBY 12:00:00 [proof] counterexample trace: engine_0/trace.vcd
SBY 12:00:00 [proof] report stored in engine_0/summary.txt
"""
    result = SymbiYosysAdapter().parse_result(work_root, ["proof.sby"], "", "", combined)
    assert result["status"] == "failed"
    assert "uart_start_seen" in result["property_ids"]
    assert "engine_0/trace.vcd" in result["counterexample_paths"]
    assert "engine_0/summary.txt" in result["report_paths"]


def test_adapter_parser_handles_realistic_tool_output_shapes() -> None:
    samples = {
        "verilator": (
            VerilatorAdapter(),
            "%Error: rtl/uart_rx.sv:42:13: syntax error, unexpected endmodule\n"
            "%Warning-WIDTH: rtl/uart_rx.sv:55:21: Operator ADD expects 8 bits on the RHS\n",
        ),
        "iverilog": (
            IcarusAdapter(),
            "rtl/fifo.sv:17: syntax error\n"
            "rtl/fifo.sv:18: error: malformed statement\n",
        ),
        "slang": (
            SlangAdapter(),
            "error: rtl/counter.sv:9:5: use of undeclared identifier 'next_count'\n",
        ),
        "verible": (
            VeribleAdapter(),
            "rtl/top.sv:12:7: syntax error at token \"assign\"\n",
        ),
        "symbiyosys": (
            SymbiYosysAdapter(),
            "Assert failed in p_ready_when_valid at rtl/uart_tx.sv:88: counterexample generated\n",
        ),
    }

    parsed = {name: adapter.parse_output(f"run_{name}", text) for name, (adapter, text) in samples.items()}

    assert [obs.signature for obs in parsed["verilator"]] == ["SV_EXPECTED_ENDMODULE", "SV_WIDTH_WARNING"]
    assert (parsed["verilator"][0].file or "").replace("\\", "/") == "rtl/uart_rx.sv"
    assert parsed["verilator"][0].line == 42
    assert parsed["verilator"][1].severity == "warning"
    assert [obs.signature for obs in parsed["iverilog"]] == ["SV_GENERIC_SYNTAX_ERROR", "SV_MALFORMED_STATEMENT"]
    assert parsed["slang"][0].signature == "SV_UNKNOWN_IDENTIFIER"
    assert parsed["verible"][0].signature == "SV_GENERIC_SYNTAX_ERROR"
    assert parsed["symbiyosys"][0].signature == "ASSERTION_FAILURE"
    assert (parsed["symbiyosys"][0].file or "").replace("\\", "/") == "rtl/uart_tx.sv"


def test_adapter_check_reports_actionable_open_tool_setup_guidance(sample_project: Path, monkeypatch) -> None:
    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.adapters.open_tools.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.adapters.open_tools._pyslang_available", lambda: False)
    monkeypatch.setattr("telchines.operations.shutil.which", lambda _: None)

    checks = {item["name"]: item for item in check_adapters(sample_project)["adapters"]}

    assert checks["verilator"]["status"] == "missing"
    assert checks["verilator"]["missing_binaries"] == ["verilator or verilator_bin.exe"]
    assert any("MSYS2" in item for item in checks["verilator"]["setup_diagnostics"])
    assert checks["slang"]["missing_binaries"] == ["slang or pyslang"]
    assert any("pip install pyslang" in item for item in checks["slang"]["setup_diagnostics"])
    assert checks["symbiyosys"]["missing_binaries"] == ["sby"]
    assert any("OSS CAD Suite" in item for item in checks["symbiyosys"]["setup_diagnostics"])


def test_open_tool_missing_binary_errors_include_setup_guidance(work_root: Path, monkeypatch) -> None:
    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.adapters.open_tools.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.adapters.open_tools._pyslang_available", lambda: False)

    expected = {
        VerilatorAdapter(): "MSYS2",
        SlangAdapter(): "pip install pyslang",
        SymbiYosysAdapter(): "OSS CAD Suite",
    }
    for adapter, hint in expected.items():
        with pytest.raises(AdapterExecutionError) as exc:
            adapter.run(
                f"run_{adapter.name}",
                work_root,
                ["rtl/top.sv"],
                work_root / "artifacts",
                spec=AdapterRunSpec(files=["rtl/top.sv"]),
            )
        assert "missing required binaries" in str(exc.value)
        assert hint in str(exc.value)


def test_open_tool_run_records_commands_when_binaries_available(monkeypatch, work_root: Path) -> None:
    commands: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "summary: passed\n", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, **kwargs):  # noqa: ANN001
        if len(command) > 1 and command[1] in {"--version", "-V", "-version"}:
            return Result(stdout=f"{command[0]} fake version\n")
        commands.append(command)
        return Result()

    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda binary: f"/tools/{binary}")
    monkeypatch.setattr("telchines.adapters.open_tools.shutil.which", lambda binary: f"/tools/{binary}")
    monkeypatch.setattr("telchines.adapters.base.subprocess.run", fake_run)

    spec = AdapterRunSpec(
        files=["rtl/top.sv"],
        include_dirs=["rtl/include"],
        defines=["FORMAL=1"],
        top_module="top",
        extra_args=["--quiet"],
    )
    artifacts = work_root / "artifacts"

    verilator = VerilatorAdapter().run("run_verilator", work_root, spec.files, artifacts, spec=spec)
    slang = SlangAdapter().run("run_slang", work_root, spec.files, artifacts, spec=spec)
    sby_spec = AdapterRunSpec(files=["proof.sby"], extra_args=["--prefix", "proof"])
    sby = SymbiYosysAdapter().run("run_sby", work_root, sby_spec.files, artifacts, spec=sby_spec)

    assert verilator.exit_code == 0
    assert verilator.command[:3] == ["verilator", "--lint-only", "-sv"]
    assert "-Irtl/include" in verilator.command
    assert "-DFORMAL=1" in verilator.command
    assert ["--top-module", "top"] == verilator.command[verilator.command.index("--top-module") : verilator.command.index("--top-module") + 2]
    assert slang.command[:2] == ["slang", "--lint-only"]
    assert ["--top", "top"] == slang.command[slang.command.index("--top") : slang.command.index("--top") + 2]
    assert sby.command == ["sby", "--prefix", "proof", "proof.sby"]
    assert commands == [verilator.command, slang.command, sby.command]


def test_slang_adapter_uses_pyslang_fallback_when_cli_is_missing(monkeypatch) -> None:
    monkeypatch.setattr("telchines.adapters.open_tools.shutil.which", lambda _: None)
    monkeypatch.setattr("telchines.adapters.open_tools._pyslang_available", lambda: True)

    command = SlangAdapter().build_command_from_spec(
        Path("."),
        AdapterRunSpec(files=["rtl/top.sv"], include_dirs=["rtl/include"], defines=["FORMAL=1"], top_module="top"),
    )

    assert command[1:3] == ["-m", "telchines.adapters.pyslang_runner"]
    assert "--lint-only" in command
    assert "-Irtl/include" in command
    assert "-DFORMAL=1" in command
    assert ["--top", "top"] == command[command.index("--top") : command.index("--top") + 2]


def test_iverilog_adapter_runs_compile_and_run(monkeypatch, work_root: Path) -> None:
    commands: list[list[str]] = []
    run_kwargs: list[dict[str, object]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, **kwargs):  # noqa: ANN001
        commands.append(command)
        run_kwargs.append(kwargs)
        if command[:2] == ["iverilog", "--version"]:
            return Result(0, "Icarus Verilog fake\n", "")
        if command[0] == "iverilog":
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text("compiled", encoding="utf-8")
            return Result(0, "", "")
        return Result(0, "simulation passed\n", "")

    monkeypatch.setattr("telchines.adapters.base.shutil.which", lambda _: "tool.exe")
    monkeypatch.setattr("telchines.adapters.open_tools.subprocess.run", fake_run)

    adapter = IcarusAdapter()
    execution = adapter.run(
        "run_iverilog",
        work_root,
        ["rtl/demo.sv"],
        work_root / "artifacts",
        spec=AdapterRunSpec(files=["rtl/demo.sv"], env={"API_TOKEN": "secret", "VISIBLE": "ok"}),
    )
    assert execution.exit_code == 0
    assert commands[0][:3] == ["iverilog", "-g2012", "-o"]
    assert commands[1][0] == "vvp"
    assert run_kwargs[0]["env"]["API_TOKEN"] == "secret"
    assert run_kwargs[1]["env"]["VISIBLE"] == "ok"
    assert execution.result["validation_mode"] == "compile_and_run"
    assert execution.result["compile_exit_code"] == 0
    assert execution.result["run_exit_code"] == 0
    assert execution.result["run_spec"]["env"]["API_TOKEN"] == "<redacted>"
    assert execution.result["run_spec"]["env"]["VISIBLE"] == "ok"
    assert execution.artifacts["compiled_executable"].endswith("run_iverilog.out")


def test_tool_adapter_run_passes_timeout_env_and_records_provenance(monkeypatch, work_root: Path) -> None:
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(command, **kwargs):  # noqa: ANN001
        captured["command"] = command
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr("telchines.adapters.base.subprocess.run", fake_run)
    spec = AdapterRunSpec(files=["rtl/top.sv"], extra_args=["--lint"], timeout_seconds=17, env={"TOKEN": "secret", "VISIBLE": "ok"})

    execution = EchoAdapter().run("run_echo", work_root, ["rtl/top.sv"], work_root / "artifacts", spec=spec)

    assert captured["command"] == ["echo-adapter", "--lint", "rtl/top.sv"]
    assert captured["timeout"] == 17
    assert captured["env"]["TOKEN"] == "secret"
    assert captured["env"]["VISIBLE"] == "ok"
    assert execution.result["command"] == ["echo-adapter", "--lint", "rtl/top.sv"]
    assert execution.result["run_spec"]["env"]["TOKEN"] == "<redacted>"
    assert execution.result["run_spec"]["env"]["VISIBLE"] == "ok"


def test_adapter_run_spec_expands_filelists_and_redacts_env(work_root: Path) -> None:
    rtl_dir = work_root / "rtl"
    rtl_dir.mkdir()
    (rtl_dir / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    filelist = work_root / "design.f"
    filelist.write_text(
        "\n".join(
            [
                "# comment",
                "+incdir+rtl/include",
                "+define+SIM=1",
                "rtl/top.sv // inline comment",
                "",
            ]
        ),
        encoding="utf-8",
    )

    spec = AdapterRunSpec(
        filelists=["design.f"],
        include_dirs=["rtl/local_include"],
        defines=["LOCAL=1"],
        top_module="top",
        extra_args=["-Wall"],
        env={"API_KEY": "secret", "VISIBLE": "ok"},
    )
    expanded = spec.expanded(work_root)
    command = IcarusAdapter().build_command_from_spec(work_root, spec)
    summary = spec.summary(work_root)

    assert expanded.files == ["rtl/top.sv"]
    assert expanded.include_dirs == ["rtl/local_include", "rtl/include"]
    assert expanded.defines == ["LOCAL=1", "SIM=1"]
    assert "-Irtl/include" in command
    assert "-DSIM=1" in command
    assert ["-s", "top"] == command[command.index("-s") : command.index("-s") + 2]
    assert summary["env"]["API_KEY"] == "<redacted>"
    assert summary["env"]["VISIBLE"] == "ok"


@given(
    files=st.lists(RELATIVE_PATH, max_size=6),
    filelists=st.lists(st.builds(lambda name: f"{name}.f", PATH_PART), max_size=3),
    include_dirs=st.lists(INCLUDE_DIR, max_size=5),
    defines=st.lists(DEFINE, max_size=5),
    top_module=st.one_of(st.none(), SV_IDENTIFIER),
    work_library=st.one_of(st.none(), SV_IDENTIFIER),
    extra_args=st.lists(ARG, max_size=4),
    timeout=st.one_of(st.none(), st.integers(min_value=1, max_value=300)),
)
def test_adapter_run_spec_from_mapping_round_trips_without_filelists(
    files: list[str],
    filelists: list[str],
    include_dirs: list[str],
    defines: list[str],
    top_module: str | None,
    work_library: str | None,
    extra_args: list[str],
    timeout: int | None,
) -> None:
    payload = {
        "files": files,
        "filelists": filelists,
        "include_dirs": include_dirs,
        "defines": defines,
        "top_module": top_module,
        "work_library": work_library,
        "standard": "systemverilog",
        "timeout_seconds": timeout,
        "extra_args": extra_args,
        "env": {"VISIBLE": "ok"},
    }

    spec = AdapterRunSpec.from_mapping(payload)

    assert spec.files == files
    assert spec.filelists == filelists
    assert spec.include_dirs == include_dirs
    assert spec.defines == defines
    assert spec.top_module == top_module
    assert spec.work_library == work_library
    assert spec.timeout_seconds == timeout
    assert spec.extra_args == extra_args
    assert spec.env == {"VISIBLE": "ok"}


@given(
    files=st.lists(RELATIVE_PATH, min_size=1, max_size=5),
    include_dirs=st.lists(INCLUDE_DIR, max_size=4),
    defines=st.lists(DEFINE, max_size=4),
)
@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_adapter_run_spec_expands_filelist_entries_preserving_unique_order(
    work_root: Path,
    files: list[str],
    include_dirs: list[str],
    defines: list[str],
) -> None:
    filelist = work_root / "generated.f"
    filelist.write_text(
        "\n".join(
            [
                "# generated filelist",
                "",
                *[f"+incdir+{path}" for path in include_dirs],
                *[f"+define+{define}" for define in defines],
                *[f"{path} // source" for path in files],
                *files[:2],
            ]
        ),
        encoding="utf-8",
    )

    spec = AdapterRunSpec(files=files[:1], filelists=["generated.f"], include_dirs=include_dirs[:1], defines=defines[:1])
    expanded = spec.expanded(work_root)

    assert expanded.files == _unique([*files[:1], *files, *files[:2]])
    assert expanded.include_dirs == _unique([*include_dirs[:1], *include_dirs])
    assert expanded.defines == _unique([*defines[:1], *defines])


def test_adapter_run_spec_missing_filelist_fails(work_root: Path) -> None:
    with pytest.raises(AdapterExecutionError, match="filelist does not exist"):
        AdapterRunSpec(filelists=["missing.f"]).expanded(work_root)


@given(secret_key=st.sampled_from(["API_KEY", "auth_token", "PASSWORD", "client_secret"]), visible_value=st.text(min_size=0, max_size=12))
def test_adapter_run_spec_redacts_secret_like_env_keys_without_mutating(secret_key: str, visible_value: str) -> None:
    env = {secret_key: "super-secret", "VISIBLE": visible_value}
    spec = AdapterRunSpec(env=env)

    summary = spec.summary(Path("."))

    assert summary["env"][secret_key] == "<redacted>"
    assert summary["env"]["VISIBLE"] == visible_value
    assert env[secret_key] == "super-secret"


@given(
    files=st.lists(RELATIVE_PATH, min_size=1, max_size=4),
    include_dirs=st.lists(INCLUDE_DIR, max_size=3),
    defines=st.lists(DEFINE, max_size=3),
    top_module=SV_IDENTIFIER,
    extra_args=st.lists(ARG, max_size=3),
)
def test_builtin_adapter_command_construction_from_run_spec(
    files: list[str],
    include_dirs: list[str],
    defines: list[str],
    top_module: str,
    extra_args: list[str],
) -> None:
    spec = AdapterRunSpec(files=files, include_dirs=include_dirs, defines=defines, top_module=top_module, extra_args=extra_args)

    iverilog = IcarusAdapter().build_command_from_spec(Path("."), spec)
    verilator = VerilatorAdapter().build_command_from_spec(Path("."), spec)
    slang = SlangAdapter().build_command_from_spec(Path("."), spec)
    verible = VeribleAdapter().build_command_from_spec(Path("."), spec)
    sby = SymbiYosysAdapter().build_command_from_spec(Path("."), spec)

    assert iverilog[:2] == ["iverilog", "-g2012"]
    assert ["-s", top_module] == iverilog[iverilog.index("-s") : iverilog.index("-s") + 2]
    assert ["--top-module", top_module] == verilator[verilator.index("--top-module") : verilator.index("--top-module") + 2]
    assert ["--top", top_module] == slang[slang.index("--top") : slang.index("--top") + 2]
    for include_dir in include_dirs:
        assert f"-I{include_dir}" in iverilog
        assert f"-I{include_dir}" in verilator
        assert f"-I{include_dir}" in slang
        assert f"-I{include_dir}" not in verible
        assert f"-I{include_dir}" not in sby
    for define in defines:
        assert f"-D{define}" in iverilog
        assert f"-D{define}" in verilator
        assert f"-D{define}" in slang
        assert f"-D{define}" not in verible
        assert f"-D{define}" not in sby
    for arg in extra_args:
        assert arg in iverilog
        assert arg in verilator
        assert arg in slang
        assert arg in verible
        assert arg in sby
