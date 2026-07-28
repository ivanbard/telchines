from __future__ import annotations

import copy
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from telchines.errors import ConfigError
from telchines.utils import ensure_directory, read_json, write_json

SETUP_VERSION = 1
ENV_VAR_NAME = "TELCHINES_CONFIG_DIR"
API_KEY_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SHELL_HISTORY_LIMIT = 500


def settings_path() -> Path:
    """Return the per-user setup location, with an override for automation."""
    configured = os.environ.get(ENV_VAR_NAME)
    if configured:
        return Path(configured).expanduser().resolve() / "settings.json"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "telchines" / "settings.json"


def shell_history_path() -> Path:
    return settings_path().with_name("shell_history.txt")


def default_model_policy() -> dict[str, Any]:
    return {
        "default_provider_by_capability": {"repair": "heuristic", "generation": "heuristic"},
        "providers": {"heuristic": {"kind": "heuristic", "capabilities": ["repair", "generation"]}},
    }


@dataclass(slots=True)
class UserSetup:
    completed: bool = False
    model_mode: str = "local"
    no_egress: bool = True
    allow_local_commands: bool = False
    artifact_storage_acknowledged: bool = False
    shell_history_enabled: bool = False
    model_policy: dict[str, Any] | None = None
    version: int = SETUP_VERSION

    @classmethod
    def load(cls) -> "UserSetup | None":
        path = settings_path()
        if not path.exists():
            return None
        payload = read_json(path)
        if not isinstance(payload, dict) or payload.get("version") != SETUP_VERSION:
            return None
        value = cls(
            completed=bool(payload.get("completed")),
            model_mode=str(payload.get("model_mode", "local")),
            no_egress=bool(payload.get("no_egress", True)),
            allow_local_commands=bool(payload.get("allow_local_commands", False)),
            artifact_storage_acknowledged=bool(payload.get("artifact_storage_acknowledged", False)),
            shell_history_enabled=bool(payload.get("shell_history_enabled", False)),
            model_policy=payload.get("model_policy") if isinstance(payload.get("model_policy"), dict) else None,
            version=int(payload.get("version", SETUP_VERSION)),
        )
        value.validate()
        return value

    def validate(self) -> None:
        if self.model_mode not in {"local", "hybrid", "remote"}:
            raise ConfigError("setup model_mode must be local, hybrid, or remote")
        if not isinstance(self.model_policy, dict) or not self.model_policy.get("providers"):
            raise ConfigError("setup model_policy must include providers")
        providers = self.model_policy.get("providers", {})
        if not isinstance(providers, dict):
            raise ConfigError("setup model_policy.providers must be an object")
        for name, provider in providers.items():
            if not isinstance(name, str) or not isinstance(provider, dict):
                raise ConfigError("setup providers must have string names and object configuration")
            api_key_env = provider.get("api_key_env")
            if api_key_env is not None and (not isinstance(api_key_env, str) or not API_KEY_ENV_RE.fullmatch(api_key_env)):
                raise ConfigError("API-key environment variable names must be uppercase identifiers; never paste an API key here")

    def save(self) -> None:
        self.validate()
        path = settings_path()
        ensure_directory(path.parent)
        write_json(
            path,
            {
                "version": self.version,
                "completed": self.completed,
                "model_mode": self.model_mode,
                "no_egress": self.no_egress,
                "allow_local_commands": self.allow_local_commands,
                "artifact_storage_acknowledged": self.artifact_storage_acknowledged,
                "shell_history_enabled": self.shell_history_enabled,
                "model_policy": self.model_policy,
            },
        )

    def project_defaults(self) -> dict[str, Any]:
        self.validate()
        return {
            "model_mode": self.model_mode,
            "no_egress": self.no_egress,
            "allow_local_commands": self.allow_local_commands,
            "model_policy": copy.deepcopy(self.model_policy),
        }


def global_project_defaults() -> dict[str, Any] | None:
    setup = UserSetup.load()
    return setup.project_defaults() if setup and setup.completed else None


def shell_history_status() -> dict[str, object]:
    setup = UserSetup.load()
    path = shell_history_path()
    enabled = bool(setup and setup.shell_history_enabled)
    entries = load_shell_history() if enabled else []
    return {"enabled": enabled, "path": str(path), "entry_count": len(entries), "limit": SHELL_HISTORY_LIMIT}


def set_shell_history_enabled(enabled: bool) -> dict[str, object]:
    setup = UserSetup.load()
    if setup is None:
        raise ConfigError("run `tel setup` before enabling shell history")
    setup.shell_history_enabled = enabled
    setup.save()
    return shell_history_status()


def load_shell_history() -> list[str]:
    setup = UserSetup.load()
    path = shell_history_path()
    if not setup or not setup.shell_history_enabled or not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()][-SHELL_HISTORY_LIMIT:]


def append_shell_history(command: str) -> None:
    command = command.strip()
    if not command:
        return
    entries = load_shell_history()
    if entries and entries[-1] == command:
        return
    entries.append(command)
    ensure_directory(shell_history_path().parent)
    shell_history_path().write_text("\n".join(entries[-SHELL_HISTORY_LIMIT:]) + "\n", encoding="utf-8")


def clear_shell_history() -> None:
    path = shell_history_path()
    if path.exists():
        path.unlink()


def _ask_bool(prompt: str, *, default: bool) -> bool:
    return typer.confirm(prompt, default=default)


def _ask_text(prompt: str, *, default: str | None = None) -> str:
    return typer.prompt(prompt, default=default).strip()


def _ask_api_key_env(prompt: str, *, default: str | None = None) -> str:
    value = _ask_text(prompt, default=default)
    if not API_KEY_ENV_RE.fullmatch(value):
        raise ConfigError("API-key environment variable names must be uppercase identifiers; never paste an API key here")
    return value


def _remote_provider(kind: str) -> dict[str, Any]:
    if kind == "openai":
        model = _ask_text("Model", default="gpt-5.5")
        env = _ask_api_key_env("API-key environment variable", default="OPENAI_API_KEY")
        return {"name": "openai", "config": {"kind": "openai_compatible", "capabilities": ["repair", "generation"], "base_url": "https://api.openai.com/v1", "endpoint": "responses", "model": model, "api_key_env": env, "auth": "bearer", "timeout_seconds": 60}}
    if kind == "anthropic":
        model = _ask_text("Model", default="claude-sonnet-5")
        env = _ask_api_key_env("API-key environment variable", default="ANTHROPIC_API_KEY")
        return {"name": "anthropic", "config": {"kind": "anthropic", "capabilities": ["repair", "generation"], "base_url": "https://api.anthropic.com/v1", "model": model, "api_key_env": env, "timeout_seconds": 90}}
    base_url = _ask_text("OpenAI-compatible base URL")
    model = _ask_text("Model")
    env = _ask_api_key_env("API-key environment variable")
    return {"name": "remote", "config": {"kind": "openai_compatible", "capabilities": ["repair", "generation"], "base_url": base_url, "model": model, "api_key_env": env, "auth": "bearer", "timeout_seconds": 60}}


def _provider_policy() -> tuple[dict[str, Any], bool]:
    typer.echo("\nChoose a provider: 1) offline heuristic  2) OpenAI  3) Anthropic  4) OpenAI-compatible  5) local server")
    choice = _ask_text("Provider", default="1")
    if choice not in {"1", "2", "3", "4", "5"}:
        raise ConfigError("choose a provider number from 1 through 5")
    if choice == "1":
        return default_model_policy(), False
    if choice in {"2", "3", "4"}:
        if not _ask_bool("Allow Telchines to send project context to this remote provider?", default=False):
            typer.echo("Remote access was not approved; using the offline heuristic provider.")
            return default_model_policy(), False
        selected = _remote_provider({"2": "openai", "3": "anthropic", "4": "compatible"}[choice])
        return {"default_provider_by_capability": {"repair": selected["name"], "generation": selected["name"]}, "providers": {selected["name"]: selected["config"]}}, True
    base_url = _ask_text("Local server base URL", default="http://127.0.0.1:11434/v1")
    model = _ask_text("Model")
    config = {"kind": "openai_compatible", "capabilities": ["repair", "generation"], "base_url": base_url, "model": model, "auth": "none", "timeout_seconds": 60}
    return {"default_provider_by_capability": {"repair": "local-openai", "generation": "local-openai"}, "providers": {"local-openai": config}}, False


def _credential_hint(setup: UserSetup) -> str | None:
    providers = setup.model_policy.get("providers", {}) if setup.model_policy else {}
    for config in providers.values():
        if isinstance(config, dict) and config.get("api_key_env"):
            name = str(config["api_key_env"])
            return f"Set {name} before use, for example in PowerShell: $env:{name} = \"your-key\""
    return None


def run_setup(*, offer_check: bool = True) -> str:
    """Run the shared terminal wizard and return a concise completion summary."""
    typer.echo("\nTelchines setup\nConfigure defaults now; projects are initialized separately.")
    policy, remote_enabled = _provider_policy()
    allow_local_commands = _ask_bool("Allow future projects to run configured local-command providers?", default=False)
    acknowledged = _ask_bool("Acknowledge that project prompts, source context, and model responses may be stored under .tel/?", default=False)
    if not acknowledged:
        raise ConfigError("artifact storage acknowledgement is required to finish setup")
    shell_history_enabled = _ask_bool("Save shell command history in your private Telchines settings?", default=False)
    setup = UserSetup(
        completed=True,
        model_mode="hybrid" if remote_enabled and allow_local_commands else "remote" if remote_enabled else "local",
        no_egress=not remote_enabled,
        allow_local_commands=allow_local_commands,
        artifact_storage_acknowledged=True,
        shell_history_enabled=shell_history_enabled,
        model_policy=policy,
    )
    setup.save()
    lines = ["Setup complete.", f"Settings: {settings_path()}"]
    hint = _credential_hint(setup)
    if hint:
        lines.append(hint)
    if offer_check and _ask_bool("Test this provider now?", default=False):
        lines.append(_check_setup_provider(setup))
    lines.append("In your repository, run: tel project init .")
    lines.append("Run `tel setup` or `/setup` any time to change these defaults.")
    return "\n".join(lines)


def _check_setup_provider(setup: UserSetup) -> str:
    """Use a disposable project so setup can validate a provider before a repo exists."""
    from telchines.config import ProjectConfig
    from telchines.operations import check_providers

    provider_name = next(iter(setup.model_policy["providers"]))
    if provider_name == "heuristic":
        return "Provider check: offline heuristic is ready."
    with tempfile.TemporaryDirectory(prefix="telchines-setup-") as root:
        config = ProjectConfig.init_project(Path(root))
        defaults = setup.project_defaults()
        config.model_mode = str(defaults["model_mode"])
        config.no_egress = bool(defaults["no_egress"])
        config.allow_local_commands = bool(defaults["allow_local_commands"])
        config.project.model_policy = defaults["model_policy"]
        config.save()
        try:
            result = check_providers(Path(root), provider_name=provider_name, live=True)
        except Exception as exc:  # setup must remain completable when a service is unavailable
            return f"Provider check could not run: {exc}. Run `tel providers check {provider_name}` after project init."
    return "Provider check: passed." if result.get("status") == "passed" else f"Provider check: {result.get('status')}. Run `tel providers check {provider_name}` after project init."
