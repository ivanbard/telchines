from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

from telchines.config import ProjectConfig, SUPPORTED_REASONING_LEVELS
from telchines.errors import ConfigError


OPENAI_REASONING_LEVELS = ["auto", "none", "minimal", "low", "medium", "high", "xhigh"]
ANTHROPIC_REASONING_LEVELS = ["auto", "low", "medium", "high"]
LOCAL_REASONING_LEVELS = sorted(SUPPORTED_REASONING_LEVELS)

PRESET_MODELS: dict[str, list[str]] = {
    "openai_compatible": ["gpt-5.5", "gpt-5.1", "gpt-4.1", "gpt-4.1-mini"],
    "anthropic": ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
    "local_command": [],
    "agent_runtime": [],
    "heuristic": ["heuristic"],
}


def list_model_options(config: ProjectConfig, *, live: bool = True) -> dict[str, object]:
    providers = config.project.model_policy.get("providers", {})
    defaults = config.default_provider_by_capability()
    options: list[dict[str, object]] = []
    for provider_name in sorted(providers):
        provider_config = providers[provider_name]
        if not isinstance(provider_config, dict):
            continue
        options.append(_provider_model_option(provider_name, provider_config, providers, defaults, live=live))
    return {
        "default_provider_by_capability": defaults,
        "reasoning_levels": sorted(SUPPORTED_REASONING_LEVELS),
        "providers": options,
    }


def set_default_provider(config: ProjectConfig, capability: str, provider_name: str) -> dict[str, object]:
    capability = capability.strip()
    if capability not in {"repair", "generation"}:
        raise ConfigError("capability must be repair or generation")
    providers = config.project.model_policy.get("providers", {})
    provider_config = providers.get(provider_name)
    if not isinstance(provider_config, dict):
        raise ConfigError(f"provider {provider_name} is not configured")
    if capability not in config.provider_capabilities(provider_name, provider_config):
        raise ConfigError(f"provider {provider_name} does not support {capability}")
    defaults = dict(config.default_provider_by_capability())
    defaults[capability] = provider_name
    config.project.model_policy["default_provider_by_capability"] = defaults
    config.save()
    return {
        "status": "updated",
        "capability": capability,
        "provider": provider_name,
        "default_provider_by_capability": defaults,
    }


def set_provider_model(config: ProjectConfig, provider_name: str, model: str) -> dict[str, object]:
    model = model.strip()
    if not model:
        raise ConfigError("model must be non-empty")
    provider_config = _mutable_provider_config(config, provider_name)
    if provider_config.get("kind") in {"heuristic", "agent_runtime"}:
        raise ConfigError(f"provider {provider_name} does not own a directly selectable model")
    provider_config["model"] = model
    provider_config["model_source"] = "manual"
    config.save()
    return {"status": "updated", "provider": provider_name, "model": model, "model_source": "manual"}


def set_provider_reasoning(config: ProjectConfig, provider_name: str, level: str) -> dict[str, object]:
    level = level.strip()
    if level not in SUPPORTED_REASONING_LEVELS:
        raise ConfigError(f"reasoning level must be one of: {', '.join(sorted(SUPPORTED_REASONING_LEVELS))}")
    provider_config = _mutable_provider_config(config, provider_name)
    provider_config["reasoning_level"] = level
    config.save()
    return {
        "status": "updated",
        "provider": provider_name,
        "reasoning_level": level,
        "reasoning_supported": _reasoning_supported(provider_config),
    }


def provider_model_metadata(provider_name: str, provider_config: dict[str, Any], providers: dict[str, Any] | None = None) -> dict[str, object]:
    providers = providers or {}
    kind = str(provider_config.get("kind", ""))
    if kind == "agent_runtime":
        base_name = str(provider_config.get("base_provider") or "")
        base_config = providers.get(base_name)
        if isinstance(base_config, dict):
            metadata = provider_model_metadata(base_name, base_config, providers)
            return {
                **metadata,
                "provider": provider_name,
                "base_provider": base_name,
                "model_source": "delegated",
            }
        return {
            "provider": provider_name,
            "model": None,
            "model_source": "delegated",
            "reasoning_level": "auto",
            "reasoning_supported": False,
            "reasoning_wire_format": "none",
            "model_warnings": [f"base provider is not configured: {base_name}"],
        }
    return {
        "provider": provider_name,
        "model": provider_config.get("model") or ("heuristic" if kind == "heuristic" else None),
        "model_source": provider_config.get("model_source") or ("configured" if provider_config.get("model") else "preset"),
        "reasoning_level": str(provider_config.get("reasoning_level", "auto")),
        "reasoning_summary": provider_config.get("reasoning_summary"),
        "reasoning_supported": _reasoning_supported(provider_config),
        "reasoning_wire_format": _resolved_reasoning_wire_format(provider_config),
        "model_warnings": _model_warnings(provider_config),
    }


def openai_models_url(provider_config: dict[str, Any]) -> str:
    base_url = str(provider_config["base_url"]).rstrip("/")
    return f"{base_url}/models"


def anthropic_models_url(provider_config: dict[str, Any]) -> str:
    base_url = str(provider_config.get("base_url", "https://api.anthropic.com/v1")).rstrip("/")
    return f"{base_url}/models"


def _provider_model_option(
    provider_name: str,
    provider_config: dict[str, Any],
    providers: dict[str, Any],
    defaults: dict[str, str],
    *,
    live: bool,
) -> dict[str, object]:
    metadata = provider_model_metadata(provider_name, provider_config, providers)
    discovered, source, error_text = _discover_models(provider_name, provider_config, live=live)
    configured_model = metadata.get("model")
    preset_models = PRESET_MODELS.get(str(provider_config.get("kind")), [])
    models = _unique_strings([*(discovered or []), configured_model, *preset_models])
    if not models and provider_config.get("kind") == "local_command":
        models = ["wrapper-managed"]
    return {
        "name": provider_name,
        "kind": provider_config.get("kind"),
        "capabilities": _provider_capabilities(provider_config, defaults, provider_name),
        "default_for": [capability for capability, name in defaults.items() if name == provider_name],
        "models": models,
        "model": configured_model,
        "model_source": source if discovered else metadata.get("model_source"),
        "discovery_status": "passed" if discovered else ("skipped" if not live else "fallback"),
        "discovery_error": error_text,
        "reasoning_level": metadata.get("reasoning_level"),
        "reasoning_summary": metadata.get("reasoning_summary"),
        "reasoning_supported": metadata.get("reasoning_supported"),
        "reasoning_wire_format": metadata.get("reasoning_wire_format"),
        "reasoning_levels": _reasoning_levels(provider_config),
        "model_warnings": metadata.get("model_warnings", []),
        "base_provider": metadata.get("base_provider") or provider_config.get("base_provider"),
    }


def _discover_models(provider_name: str, provider_config: dict[str, Any], *, live: bool) -> tuple[list[str], str, str | None]:
    if not live:
        return [], "configured", None
    kind = provider_config.get("kind")
    try:
        if kind == "openai_compatible":
            return _discover_openai_compatible_models(provider_name, provider_config), "live", None
        if kind == "anthropic":
            return _discover_anthropic_models(provider_name, provider_config), "live", None
    except (OSError, ValueError, KeyError, error.URLError) as exc:
        return [], "configured", str(exc)
    return [], "configured", None


def _discover_openai_compatible_models(provider_name: str, provider_config: dict[str, Any]) -> list[str]:
    auth = str(provider_config.get("auth", "bearer"))
    headers = {"Content-Type": "application/json"}
    if auth == "bearer":
        api_key_env = str(provider_config.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(f"missing credentials: set {api_key_env}")
        headers["Authorization"] = f"Bearer {api_key}"
    for key, value in provider_config.get("headers", {}).items():
        if str(key).lower() == "authorization":
            raise ValueError(f"provider {provider_name} custom headers cannot override Authorization")
        headers[str(key)] = str(value)
    return _models_from_response(_get_json(openai_models_url(provider_config), headers, int(provider_config.get("timeout_seconds", 30))))


def _discover_anthropic_models(provider_name: str, provider_config: dict[str, Any]) -> list[str]:
    api_key_env = str(provider_config.get("api_key_env", "ANTHROPIC_API_KEY"))
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"missing credentials: set {api_key_env}")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": str(provider_config.get("anthropic_version", "2023-06-01")),
    }
    for key, value in provider_config.get("headers", {}).items():
        lowered = str(key).lower()
        if lowered in {"x-api-key", "anthropic-version", "content-type"}:
            raise ValueError(f"provider {provider_name} custom headers cannot override Anthropic transport headers")
        headers[str(key)] = str(value)
    return _models_from_response(_get_json(anthropic_models_url(provider_config), headers, int(provider_config.get("timeout_seconds", 30))))


def _get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    http_request = request.Request(url, headers=headers, method="GET")
    with request.urlopen(http_request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _models_from_response(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []
    models: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    return models


def _mutable_provider_config(config: ProjectConfig, provider_name: str) -> dict[str, Any]:
    providers = config.project.model_policy.get("providers", {})
    provider_config = providers.get(provider_name)
    if not isinstance(provider_config, dict):
        raise ConfigError(f"provider {provider_name} is not configured")
    return provider_config


def _provider_capabilities(provider_config: dict[str, Any], defaults: dict[str, str], provider_name: str) -> list[str]:
    capabilities = provider_config.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        return [str(item) for item in capabilities]
    return [capability for capability, default_provider in defaults.items() if default_provider == provider_name] or ["repair"]


def _reasoning_levels(provider_config: dict[str, Any]) -> list[str]:
    kind = provider_config.get("kind")
    if kind == "openai_compatible":
        return OPENAI_REASONING_LEVELS
    if kind == "anthropic":
        return ANTHROPIC_REASONING_LEVELS
    if kind == "agent_runtime":
        return LOCAL_REASONING_LEVELS
    if kind == "local_command":
        return LOCAL_REASONING_LEVELS
    return ["auto"]


def _reasoning_supported(provider_config: dict[str, Any]) -> bool:
    kind = provider_config.get("kind")
    if kind == "openai_compatible":
        return _resolved_reasoning_wire_format(provider_config) != "none"
    if kind == "anthropic":
        return True
    if kind == "local_command":
        return True
    return False


def _resolved_reasoning_wire_format(provider_config: dict[str, Any]) -> str:
    configured = provider_config.get("reasoning_wire_format")
    if configured and configured != "auto":
        return str(configured)
    kind = provider_config.get("kind")
    if kind == "openai_compatible":
        endpoint = str(provider_config.get("endpoint", "chat/completions")).strip("/")
        if endpoint == "responses":
            return "openai_responses"
        if bool(provider_config.get("supports_reasoning_effort", False)):
            return "openai_chat"
        return "none"
    if kind == "anthropic":
        return "anthropic_adaptive"
    if kind == "local_command":
        return "none"
    return "none"


def _model_warnings(provider_config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    model = str(provider_config.get("model") or "")
    kind = provider_config.get("kind")
    wire = _resolved_reasoning_wire_format(provider_config)
    if kind == "openai_compatible" and provider_config.get("reasoning_level", "auto") != "auto" and wire == "none":
        warnings.append("reasoning_level is configured but this OpenAI-compatible provider is not marked reasoning-capable")
    if kind == "anthropic" and provider_config.get("reasoning_level") in {"none", "minimal", "xhigh"}:
        warnings.append("configured reasoning level may not be supported by Anthropic adaptive thinking")
    if model.endswith("-latest"):
        warnings.append("model alias may move over time; pin a dated model for reproducibility")
    return warnings


def _unique_strings(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
