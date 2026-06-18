from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from telchines.errors import ConfigError, ProjectNotInitializedError
from telchines.models import VerificationProject
from telchines.utils import dataclass_to_dict, ensure_directory, read_json, stable_id, utc_now, write_json

SUPPORTED_MODEL_MODES = {"local", "hybrid", "remote"}
SUPPORTED_ADAPTERS = {"verilator", "iverilog", "slang", "verible", "symbiyosys", "fixture"}
SUPPORTED_PROVIDER_KINDS = {"heuristic", "openai_compatible", "local_command"}


def default_generation_config() -> dict[str, Any]:
    return {
        "sva": {
            "output_dir": ".tel/artifacts/generated",
            "filename_template": "{module}_assertions.sv",
            "clock_names": ["clk", "clock"],
            "reset_names": ["rst_n", "reset_n", "rst", "reset"],
            "active_low_reset_names": ["rst_n", "reset_n"],
            "validation_adapters": ["slang", "verilator"],
        },
        "cocotb": {
            "output_dir": ".tel/artifacts/generated/cocotb",
            "test_file_template": "test_{module}.py",
            "manifest_file_template": "{module}_cocotb_manifest.json",
            "clock_names": ["clk", "clock"],
            "reset_names": ["rst_n", "reset_n", "rst", "reset"],
            "active_low_reset_names": ["rst_n", "reset_n"],
        },
    }


def merge_generation_config(value: dict[str, Any] | None) -> dict[str, Any]:
    merged = default_generation_config()
    if not isinstance(value, dict):
        return merged
    for section_name in ("sva", "cocotb"):
        section = value.get(section_name)
        if isinstance(section, dict):
            merged[section_name].update(section)
    return merged


@dataclass(slots=True)
class ProjectConfig:
    project: VerificationProject
    store_dir: str = ".tel"
    index_dir: str = ".tel/index"
    artifacts_dir: str = ".tel/artifacts"
    model_mode: str = "hybrid"
    no_egress: bool = False
    adapters: list[str] = field(default_factory=lambda: ["verilator", "iverilog", "slang", "verible", "symbiyosys"])
    retrieval: dict[str, Any] = field(
        default_factory=lambda: {
            "chunk_lines": 20,
            "max_hits": 5,
            "external_roots": [],
            "external_index_dir": ".tel/external-index",
            "include_patterns": ["**/*"],
            "exclude_patterns": [],
            "aliases": {},
        }
    )
    generation: dict[str, Any] = field(default_factory=default_generation_config)

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
            "generation": self.generation,
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
            retrieval=payload.get(
                "retrieval",
                {
                    "chunk_lines": 20,
                    "max_hits": 5,
                    "external_roots": [],
                    "external_index_dir": ".tel/external-index",
                    "include_patterns": ["**/*"],
                    "exclude_patterns": [],
                    "aliases": {},
                },
            ),
            generation=payload.get("generation", default_generation_config()),
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
                    "generation": "heuristic",
                },
                "providers": {
                    "heuristic": {
                        "kind": "heuristic",
                        "capabilities": ["repair", "generation"],
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
        external_index_dir = self.retrieval.get("external_index_dir", ".tel/external-index")
        if not isinstance(external_index_dir, str) or not external_index_dir.strip():
            raise ConfigError("retrieval.external_index_dir must be a non-empty string")
        if Path(external_index_dir).is_absolute():
            raise ConfigError("retrieval.external_index_dir must be relative to the project root")
        external_roots = self.retrieval.get("external_roots", [])
        if not isinstance(external_roots, list) or any(not isinstance(item, str) or not item.strip() for item in external_roots):
            raise ConfigError("retrieval.external_roots must be a list of non-empty strings")
        for root_value in external_roots:
            if Path(root_value).is_absolute():
                raise ConfigError("retrieval.external_roots entries must be relative to the project root")
        for key in ("include_patterns", "exclude_patterns"):
            patterns = self.retrieval.get(key, ["**/*"] if key == "include_patterns" else [])
            if not isinstance(patterns, list) or any(not isinstance(item, str) or not item.strip() for item in patterns):
                raise ConfigError(f"retrieval.{key} must be a list of non-empty strings")
            if any(Path(item).is_absolute() for item in patterns):
                raise ConfigError(f"retrieval.{key} entries must be relative glob patterns")
        aliases = self.retrieval.get("aliases", {})
        if not isinstance(aliases, dict):
            raise ConfigError("retrieval.aliases must be an object")
        for alias_key, alias_values in aliases.items():
            if not isinstance(alias_key, str) or not alias_key.strip():
                raise ConfigError("retrieval.aliases keys must be non-empty strings")
            if not isinstance(alias_values, list) or any(not isinstance(item, str) or not item.strip() for item in alias_values):
                raise ConfigError("retrieval.aliases values must be lists of non-empty strings")
        if not isinstance(self.generation, dict):
            raise ConfigError("generation must be an object")
        self.generation = merge_generation_config(self.generation)
        self._validate_generation_section("sva", path_keys=("output_dir",), template_keys=("filename_template",))
        self._validate_generation_section(
            "cocotb",
            path_keys=("output_dir",),
            template_keys=("test_file_template", "manifest_file_template"),
        )
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
                parsed_base_url = urlparse(provider_config["base_url"])
                if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
                    raise ConfigError(f"provider {provider_name} base_url must be an http(s) URL")
                if not isinstance(provider_config.get("model"), str) or not provider_config["model"].strip():
                    raise ConfigError(f"provider {provider_name} must define model")
                endpoint = provider_config.get("endpoint", "chat/completions")
                if not isinstance(endpoint, str) or not endpoint.strip() or "://" in endpoint:
                    raise ConfigError(f"provider {provider_name} endpoint must be a relative path")
                headers = provider_config.get("headers", {})
                if not isinstance(headers, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
                    raise ConfigError(f"provider {provider_name} headers must be an object of string pairs")
                if any(key.lower() == "authorization" for key in headers):
                    raise ConfigError(f"provider {provider_name} custom headers cannot override Authorization")

            if kind == "local_command":
                command = provider_config.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise ConfigError(f"provider {provider_name} must define command")
                output_limit = provider_config.get("output_limit_chars")
                if output_limit is not None and (not isinstance(output_limit, int) or output_limit < 1024):
                    raise ConfigError(f"provider {provider_name} output_limit_chars must be an integer of at least 1024")
                args = provider_config.get("args", [])
                if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                    raise ConfigError(f"provider {provider_name} args must be a list of strings")
                env = provider_config.get("env", {})
                if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
                    raise ConfigError(f"provider {provider_name} env must be an object of string pairs")

    def _validate_generation_section(self, section_name: str, *, path_keys: tuple[str, ...], template_keys: tuple[str, ...]) -> None:
        section = self.generation.get(section_name)
        if not isinstance(section, dict):
            raise ConfigError(f"generation.{section_name} must be an object")
        for key in path_keys:
            value = section.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"generation.{section_name}.{key} must be a non-empty string")
            if Path(value).is_absolute():
                raise ConfigError(f"generation.{section_name}.{key} must be relative to the project root")
        for key in template_keys:
            value = section.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"generation.{section_name}.{key} must be a non-empty string")
            template_path = Path(value)
            if template_path.is_absolute() or template_path.name != value or ".." in template_path.parts:
                raise ConfigError(f"generation.{section_name}.{key} must be a file-name template")
            try:
                value.format(module="example", dut_stem="example", rtl_stem="example")
            except (KeyError, IndexError, ValueError) as exc:
                raise ConfigError(f"generation.{section_name}.{key} has an invalid template placeholder") from exc
        for key in ("clock_names", "reset_names", "active_low_reset_names"):
            values = section.get(key, [])
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ConfigError(f"generation.{section_name}.{key} must be a list of non-empty strings")
        if section_name == "sva":
            adapters = section.get("validation_adapters", [])
            if not isinstance(adapters, list) or any(not isinstance(item, str) or not item.strip() for item in adapters):
                raise ConfigError("generation.sva.validation_adapters must be a list of non-empty strings")

    def save(self) -> None:
        self.validate()
        write_json(self.config_path, self.to_dict())
