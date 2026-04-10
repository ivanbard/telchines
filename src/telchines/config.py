from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telchines.errors import ConfigError, ProjectNotInitializedError
from telchines.models import VerificationProject
from telchines.utils import dataclass_to_dict, ensure_directory, read_json, stable_id, utc_now, write_json

SUPPORTED_MODEL_MODES = {"local", "hybrid", "remote"}
SUPPORTED_ADAPTERS = {"verilator", "iverilog", "verible", "symbiyosys", "fixture"}
SUPPORTED_PROVIDER_KINDS = {"heuristic", "openai_compatible", "local_command"}


@dataclass(slots=True)
class ProjectConfig:
    project: VerificationProject
    store_dir: str = ".tel"
    index_dir: str = ".tel/index"
    artifacts_dir: str = ".tel/artifacts"
    model_mode: str = "hybrid"
    no_egress: bool = False
    adapters: list[str] = field(default_factory=lambda: ["verilator", "iverilog", "verible", "symbiyosys"])
    retrieval: dict[str, Any] = field(default_factory=lambda: {"chunk_lines": 20, "max_hits": 5})

    @property
    def project_root(self) -> Path:
        return Path(self.project.root_path)

    @property
    def config_path(self) -> Path:
        return self.project_root / self.project.config_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": dataclass_to_dict(self.project),
            "store_dir": self.store_dir,
            "index_dir": self.index_dir,
            "artifacts_dir": self.artifacts_dir,
            "model_mode": self.model_mode,
            "no_egress": self.no_egress,
            "adapters": self.adapters,
            "retrieval": self.retrieval,
        }

    def default_provider_by_capability(self) -> dict[str, str]:
        model_policy = self.project.model_policy
        defaults = model_policy.get("default_provider_by_capability")
        if isinstance(defaults, dict) and defaults:
            return {str(capability): str(provider) for capability, provider in defaults.items() if str(capability).strip() and str(provider).strip()}
        repair_provider = str(model_policy.get("repair_provider", "heuristic"))
        return {"repair": repair_provider}

    def provider_capabilities(self, provider_name: str, provider_config: dict[str, Any] | None = None) -> list[str]:
        provider_config = provider_config or self.project.model_policy.get("providers", {}).get(provider_name, {})
        capabilities = provider_config.get("capabilities")
        if isinstance(capabilities, list) and capabilities:
            return [str(capability) for capability in capabilities if isinstance(capability, str) and capability.strip()]
        defaults = self.default_provider_by_capability()
        inferred = [capability for capability, default_provider in defaults.items() if default_provider == provider_name]
        return inferred or ["repair"]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectConfig":
        config = cls(
            project=VerificationProject(**payload["project"]),
            store_dir=payload["store_dir"],
            index_dir=payload["index_dir"],
            artifacts_dir=payload["artifacts_dir"],
            model_mode=payload["model_mode"],
            no_egress=payload["no_egress"],
            adapters=payload["adapters"],
            retrieval=payload["retrieval"],
        )
        config.validate()
        return config

    @classmethod
    def init_project(cls, root: Path, name: str | None = None) -> "ProjectConfig":
        root = root.resolve()
        ensure_directory(root)
        ensure_directory(root / ".tel")
        project_name = name or root.name
        project = VerificationProject(
            project_id=stable_id("proj", str(root)),
            name=project_name,
            root_path=str(root),
            created_at=utc_now(),
            model_policy={
                "default_provider_by_capability": {
                    "repair": "heuristic",
                },
                "providers": {
                    "heuristic": {
                        "kind": "heuristic",
                        "capabilities": ["repair"],
                    }
                },
            },
        )
        config = cls(project=project)
        config.save()
        return config

    @classmethod
    def load(cls, root: Path) -> "ProjectConfig":
        config_path = root.resolve() / ".tel" / "config.json"
        if not config_path.exists():
            raise ProjectNotInitializedError(f"no Telchines project found at {root.resolve()}")
        return cls.from_dict(read_json(config_path))

    @classmethod
    def discover(cls, start: Path) -> "ProjectConfig":
        current = start.resolve()
        for candidate in [current, *current.parents]:
            config_path = candidate / ".tel" / "config.json"
            if config_path.exists():
                return cls.from_dict(read_json(config_path))
        raise ProjectNotInitializedError(
            f"no Telchines project found from {start.resolve()} upward; run `tel project init` in a repository root"
        )

    def validate(self) -> None:
        if not self.project.root_path:
            raise ConfigError("project.root_path must be set")
        root = self.project_root.resolve()
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"project root does not exist: {root}")
        if self.model_mode not in SUPPORTED_MODEL_MODES:
            raise ConfigError(f"model_mode must be one of: {', '.join(sorted(SUPPORTED_MODEL_MODES))}")
        if not isinstance(self.no_egress, bool):
            raise ConfigError("no_egress must be a boolean")
        if not isinstance(self.adapters, list) or not self.adapters:
            raise ConfigError("adapters must be a non-empty list")
        invalid_adapters = [adapter for adapter in self.adapters if adapter not in SUPPORTED_ADAPTERS]
        if invalid_adapters:
            raise ConfigError(f"unsupported adapters: {', '.join(invalid_adapters)}")
        if not isinstance(self.retrieval, dict):
            raise ConfigError("retrieval must be an object")
        for key in ("chunk_lines", "max_hits"):
            value = self.retrieval.get(key)
            if not isinstance(value, int) or value <= 0:
                raise ConfigError(f"retrieval.{key} must be a positive integer")
        for path_value, field_name in (
            (self.store_dir, "store_dir"),
            (self.index_dir, "index_dir"),
            (self.artifacts_dir, "artifacts_dir"),
        ):
            if not isinstance(path_value, str) or not path_value.strip():
                raise ConfigError(f"{field_name} must be a non-empty string")
            if Path(path_value).is_absolute():
                raise ConfigError(f"{field_name} must be relative to the project root")

        model_policy = self.project.model_policy
        if not isinstance(model_policy, dict):
            raise ConfigError("project.model_policy must be an object")
        providers = model_policy.get("providers", {})
        if not isinstance(providers, dict) or not providers:
            raise ConfigError("project.model_policy.providers must be a non-empty object")

        defaults = self.default_provider_by_capability()
        if "repair" not in defaults:
            raise ConfigError("project.model_policy.default_provider_by_capability.repair must be set")
        for capability, provider_name in defaults.items():
            if provider_name not in providers:
                raise ConfigError(f"default provider for capability {capability} must reference a configured provider")

        for provider_name, provider_config in providers.items():
            if not isinstance(provider_config, dict):
                raise ConfigError(f"provider config for {provider_name} must be an object")
            kind = provider_config.get("kind")
            if kind not in SUPPORTED_PROVIDER_KINDS:
                raise ConfigError(f"provider {provider_name} has unsupported kind: {kind}")

            capabilities = self.provider_capabilities(provider_name, provider_config)
            if not capabilities:
                raise ConfigError(f"provider {provider_name} must declare at least one capability")

            timeout = provider_config.get("timeout_seconds", 30)
            if not isinstance(timeout, int) or timeout <= 0:
                raise ConfigError(f"provider {provider_name} timeout_seconds must be a positive integer")

            if kind == "openai_compatible":
                if not isinstance(provider_config.get("base_url"), str) or not provider_config["base_url"].strip():
                    raise ConfigError(f"provider {provider_name} must define base_url")
                if not isinstance(provider_config.get("model"), str) or not provider_config["model"].strip():
                    raise ConfigError(f"provider {provider_name} must define model")

            if kind == "local_command":
                command = provider_config.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise ConfigError(f"provider {provider_name} must define command")
                args = provider_config.get("args", [])
                if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                    raise ConfigError(f"provider {provider_name} args must be a list of strings")
                env = provider_config.get("env", {})
                if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
                    raise ConfigError(f"provider {provider_name} env must be an object of string pairs")

    def save(self) -> None:
        self.validate()
        write_json(self.config_path, self.to_dict())
