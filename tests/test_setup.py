from __future__ import annotations

import tempfile
from pathlib import Path

from typer.testing import CliRunner

from telchines.cli import app
from telchines.config import ProjectConfig
from telchines.errors import ConfigError
from telchines.shell import ShellSession, _dispatch_slash_command
from telchines.setup import UserSetup, append_shell_history, default_model_policy, load_shell_history, settings_path, set_shell_history_enabled, shell_history_status


try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


def test_global_setup_is_copied_when_a_project_is_initialized(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="telchines-setup-test-"))
    monkeypatch.setenv("TELCHINES_CONFIG_DIR", str(root / "settings"))
    policy = {
        "default_provider_by_capability": {"repair": "openai", "generation": "openai"},
        "providers": {
            "openai": {
                "kind": "openai_compatible",
                "capabilities": ["repair", "generation"],
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-5.5",
                "api_key_env": "OPENAI_API_KEY",
                "timeout_seconds": 60,
            }
        },
    }
    UserSetup(completed=True, model_mode="remote", no_egress=False, allow_local_commands=False, artifact_storage_acknowledged=True, model_policy=policy).save()

    config = ProjectConfig.init_project(root / "project")

    assert config.project.model_policy == policy
    assert config.model_mode == "remote"
    assert config.allow_local_commands is False
    assert settings_path().exists()


def test_setup_rejects_literal_api_key_values() -> None:
    setup = UserSetup(
        completed=True,
        artifact_storage_acknowledged=True,
        model_policy={
            "default_provider_by_capability": {"repair": "remote"},
            "providers": {"remote": {"kind": "openai_compatible", "api_key_env": "sk-not-a-variable"}},
        },
    )
    try:
        setup.validate()
    except ConfigError as exc:
        assert "never paste" in str(exc)
    else:
        raise AssertionError("expected literal key validation to fail")


def test_cli_setup_writes_offline_defaults_without_a_project(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="telchines-setup-test-"))
    monkeypatch.setenv("TELCHINES_CONFIG_DIR", str(root / "settings"))

    result = runner.invoke(app, ["setup"], input="1\nn\ny\nn\nn\n")

    assert result.exit_code == 0, result.stdout
    assert "In your repository, run: tel project init ." in result.stdout
    setup = UserSetup.load()
    assert setup is not None and setup.completed
    assert setup.model_policy == default_model_policy()
    assert setup.shell_history_enabled is False


def test_shell_history_is_opt_in_capped_and_private(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="telchines-history-test-"))
    monkeypatch.setenv("TELCHINES_CONFIG_DIR", str(root / "settings"))
    UserSetup(completed=True, artifact_storage_acknowledged=True, model_policy=default_model_policy()).save()

    assert shell_history_status()["enabled"] is False
    assert set_shell_history_enabled(True)["enabled"] is True
    for index in range(510):
        append_shell_history(f"/command {index}")
    append_shell_history("/command 509")

    history = load_shell_history()
    assert len(history) == 500
    assert history[0] == "/command 10"
    assert history[-1] == "/command 509"


def test_shell_setup_command_uses_the_shared_wizard(monkeypatch) -> None:
    monkeypatch.setattr("telchines.shell.run_setup", lambda: "Setup complete.")

    _, rendered = _dispatch_slash_command(ShellSession(cwd=Path.cwd()), "setup")

    assert rendered == "Setup complete."
