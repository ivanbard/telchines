from __future__ import annotations

from pathlib import Path

from telchines.config import ProjectConfig
from telchines.shell import ShellSession, render_help, render_welcome


def test_shell_welcome_renders_project_context(sample_project: Path) -> None:
    session = ShellSession(cwd=sample_project)
    rendered = render_welcome(session)
    config = ProjectConfig.load(sample_project)
    assert "Verification Cockpit" in rendered
    assert config.project.name in rendered
    assert "Repair Provider" in rendered


def test_shell_help_renders_core_commands() -> None:
    rendered = render_help()
    assert "/providers" in rendered
    assert "/triage --logs PATH" in rendered
    assert "/raw <slash command>" in rendered
