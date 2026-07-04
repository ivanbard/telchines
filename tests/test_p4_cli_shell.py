from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prompt_toolkit.document import Document
from typer.testing import CliRunner

from telchines.cli import app
from telchines.config import ProjectConfig
from telchines.run_store import RunStore
from telchines.shell import (
    ShellCompleter,
    ShellSession,
    _dispatch_slash_command,
    _option_command_key,
    _parse_project_init,
)
from telchines.utils import write_json

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


IDENT = st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12)
REL_PATH = st.lists(IDENT, min_size=1, max_size=3).map(lambda parts: "/".join(parts))
TEMPLATE = st.sampled_from(["basic-rtl", "cocotb", "uvm", "vivado", "quartus", "libero"])


def test_cli_project_templates_and_coverage_import(sample_project: Path, work_root: Path, monkeypatch) -> None:
    templates = runner.invoke(app, ["project", "templates"])
    assert templates.exit_code == 0
    assert "basic-rtl" in templates.stdout

    initialized = runner.invoke(app, ["project", "init", str(work_root / "vivado_proj"), "--template", "vivado"])
    assert initialized.exit_code == 0
    assert (work_root / "vivado_proj" / "constraints" / "README.md").exists()

    source = sample_project / "cov" / "questa.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Coverpoint uart_rx.start_bit_seen 0/2 rtl/uart_rx.sv\n", encoding="utf-8")
    monkeypatch.chdir(sample_project)

    imported = runner.invoke(
        app,
        ["coverage", "import", "cov/questa.txt", "--format", "questa-text", "--output", "cov/questa_imported.json"],
    )
    assert imported.exit_code == 0
    payload = json.loads(imported.stdout)
    assert payload["status"] == "imported"
    assert payload["item_count"] == 1

    planned = runner.invoke(app, ["coverage-plan", "--report", "cov/questa_imported.json"])
    assert planned.exit_code == 0
    assert json.loads(planned.stdout)["recommendation_count"] == 1


def test_cli_ci_importers_dry_run_and_store(sample_project: Path, monkeypatch) -> None:
    (sample_project / "junit.xml").write_text(
        '<testsuite name="nightly"><testcase classname="uart" name="seed_1">'
        "<failure>rtl/uart_rx.sv:42: error: timeout waiting for start bit</failure>"
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    write_json(
        sample_project / "gha.json",
        {
            "workflow_name": "nightly",
            "run_id": "123",
            "jobs": [
                {"id": 1, "name": "sim", "conclusion": "failure", "steps": [{"name": "pytest", "conclusion": "failure"}]}
            ],
        },
    )
    write_json(
        sample_project / "jenkins.json",
        {
            "fullDisplayName": "nightly #1",
            "result": "FAILURE",
            "testResult": {
                "suites": [
                    {
                        "cases": [
                            {
                                "className": "uart",
                                "name": "seed_2",
                                "status": "FAILED",
                                "errorDetails": "rtl/uart_rx.sv:42: error: timeout waiting for start bit",
                            }
                        ]
                    }
                ]
            },
        },
    )
    monkeypatch.chdir(sample_project)

    dry_run = runner.invoke(app, ["runs", "import-junit", "junit.xml", "--dry-run"])
    assert dry_run.exit_code == 0
    assert json.loads(dry_run.stdout)["dry_run"] is True

    for command, source in (
        ("import-junit", "junit.xml"),
        ("import-github-actions", "gha.json"),
        ("import-jenkins", "jenkins.json"),
    ):
        result = runner.invoke(app, ["runs", command, source])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["imported_count"] >= 1

    runs = RunStore(ProjectConfig.load(sample_project)).list_runs_by_workflow("regression_import")
    assert {run.tool.name for run in runs} >= {"junit", "github-actions", "jenkins"}


@settings(max_examples=50)
@given(path=REL_PATH, name=IDENT, template=TEMPLATE, order=st.integers(min_value=0, max_value=2))
def test_shell_project_init_parser_accepts_template_option_orders(path: str, name: str, template: str, order: int) -> None:
    parts_by_order = [
        [path, "--name", name, "--template", template],
        ["--name", name, path, "--template", template],
        ["--template", template, "--name", name, path],
    ]

    parsed_path, parsed_name, parsed_template = _parse_project_init(parts_by_order[order])

    assert parsed_path == Path(path)
    assert parsed_name == name
    assert parsed_template == template


def test_shell_routes_project_coverage_and_ci_imports(work_root: Path) -> None:
    session = ShellSession(cwd=work_root)
    should_exit, rendered = _dispatch_slash_command(session, "project init shell_proj --template uvm")

    assert should_exit is False
    assert "Project Initialized" in rendered
    assert session.cwd == (work_root / "shell_proj").resolve()
    assert (session.cwd / "tb" / "uvm").exists()

    (session.cwd / "cov" / "questa.txt").write_text("Coverpoint uart_rx.start_bit_seen 0/2 rtl/uart_rx.sv\n", encoding="utf-8")
    should_exit, rendered = _dispatch_slash_command(
        session,
        "raw coverage import cov/questa.txt --format questa-text --output cov/imported.json",
    )
    payload = json.loads(rendered)
    assert should_exit is False
    assert payload["status"] == "imported"
    assert (session.cwd / "cov" / "imported.json").exists()

    (session.cwd / "junit.xml").write_text(
        '<testsuite name="nightly"><testcase classname="uart" name="seed_1">'
        "<failure>UVM_ERROR tb/uvm/env.svh(42) @ 1ns: uvm_test_top.env [PHASE_TIMEOUT] timeout</failure>"
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    should_exit, rendered = _dispatch_slash_command(session, "raw runs import-junit junit.xml --dry-run")
    payload = json.loads(rendered)
    assert should_exit is False
    assert payload["dry_run"] is True
    assert payload["source_format"] == "junit"
    assert payload["runs"][0]["stored"] is False


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(template=st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8))
def test_shell_project_init_unknown_templates_stay_inside_workspace(work_root: Path, template: str) -> None:
    if template in {"basic-rtl", "cocotb", "uvm", "vivado", "quartus", "libero"}:
        return
    session = ShellSession(cwd=work_root)

    try:
        _dispatch_slash_command(session, f"project init generated --template {template}")
    except ValueError as exc:
        assert "unknown project template" in str(exc)
    created_paths = list((work_root / "generated").rglob("*")) if (work_root / "generated").exists() else []
    assert all(path.resolve().is_relative_to(work_root.resolve()) for path in created_paths)


def test_shell_option_completion_includes_p4_routes(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    completer = ShellCompleter(session)

    coverage_options = {item.text for item in completer.get_completions(Document("/coverage import --"), None)}
    junit_options = {item.text for item in completer.get_completions(Document("/runs import-junit --"), None)}
    project_options = {item.text for item in completer.get_completions(Document("/project init --"), None)}

    assert _option_command_key(["/coverage", "import"]) == "/coverage import"
    assert {"--format", "--output"} <= coverage_options
    assert "--dry-run" in junit_options
    assert "--template" in project_options
