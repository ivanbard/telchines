from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

from telchines.agent_runtime import LangGraphRepairRuntime, runtime_capability
from telchines.config import ProjectConfig
from telchines.errors import ConfigError, ProviderError, WorkflowInputError
from telchines.model_catalog import provider_model_metadata
from telchines.models import CocotbCandidate, CocotbPort, Observation, PatchProposal, RetrievalContext, SvaCandidate, SvaProperty, VerificationRun
from telchines.utils import stable_id

DEFAULT_LOCAL_COMMAND_OUTPUT_LIMIT_CHARS = 65536


@dataclass(slots=True)
class RepairRequest:
    task_id: str
    project_root: Path
    base_run: VerificationRun
    observations: list[Observation]
    retrieval_context: RetrievalContext
    feedback: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RepairProviderResult:
    provider_name: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    proposal: PatchProposal | None
    summary: str


@dataclass(slots=True)
class GenerationRequest:
    task_id: str
    project_root: Path
    spec_path: str
    rtl_path: str
    output_file: str
    retrieval_context: RetrievalContext
    conventions: dict[str, Any] = field(default_factory=dict)
    feedback: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class GenerationProviderResult:
    provider_name: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    candidate: SvaCandidate | None
    summary: str


@dataclass(slots=True)
class CocotbGenerationRequest:
    task_id: str
    project_root: Path
    dut_path: str
    spec_path: str | None
    output_dir: str
    intent: str
    retrieval_context: RetrievalContext
    conventions: dict[str, Any] = field(default_factory=dict)
    feedback: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class CocotbGenerationProviderResult:
    provider_name: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    candidate: CocotbCandidate | None
    summary: str


@dataclass(slots=True)
class ProviderStatus:
    name: str
    kind: str
    capabilities: list[str]
    default_for: list[str]
    allowed: bool
    blocked_reason: str = ""
    network_scope: str = ""
    auth_mode: str = ""
    model: str | None = None
    reasoning_level: str = "auto"
    reasoning_supported: bool = False
    reasoning_wire_format: str = "none"


@dataclass(slots=True)
class ProviderCheck:
    name: str
    kind: str
    status: str
    allowed: bool
    summary: str
    capabilities: list[str]
    default_for: list[str]
    checks: dict[str, Any]


class RepairProvider:
    name = "base"

    def propose_patch(self, request: RepairRequest) -> RepairProviderResult:
        raise NotImplementedError


class GenerationProvider:
    name = "base"

    def generate_sva(self, request: GenerationRequest) -> GenerationProviderResult:
        raise NotImplementedError

    def generate_cocotb(self, request: CocotbGenerationRequest) -> CocotbGenerationProviderResult:
        raise NotImplementedError


class HeuristicRepairProvider(RepairProvider):
    name = "heuristic"

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _build_repair_request_payload(request_value, self.name)
        evidence_paths = [hit.path for hit in request_value.retrieval_context.hits]
        for observation in request_value.observations:
            if observation.file is None or observation.line is None:
                continue
            if "SEMICOLON" in observation.signature:
                proposal = self._propose_semicolon_fix(request_value, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic semicolon repair")
            if observation.signature == "SV_UNKNOWN_IDENTIFIER":
                proposal = self._propose_identifier_fix(request_value, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic identifier repair")
            if observation.signature == "SV_EXPECTED_ENDMODULE":
                proposal = self._propose_endmodule_fix(request_value, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic endmodule repair")
            if observation.signature == "SV_EXPECTED_END":
                proposal = self._propose_end_fix(request_value, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic end repair")
        return self._result(request_payload, None, "heuristic provider found no supported repair")

    def _propose_semicolon_fix(self, request_value: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request_value.project_root / observation.file
        if not target.exists():
            return None
        original = target.read_text(encoding="utf-8")
        lines = original.splitlines()
        index = observation.line - 1
        if index < 0 or index >= len(lines):
            return None
        updated_line = self._append_semicolon(lines[index])
        if updated_line == lines[index]:
            return None
        new_lines = list(lines)
        new_lines[index] = updated_line
        candidate_content = "\n".join(new_lines) + ("\n" if original.endswith("\n") else "")
        return _build_patch(
            provider_name=self.name,
            request_value=request_value,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation="Added a missing semicolon at the reported error location.",
            evidence_paths=evidence_paths,
        )

    def _propose_identifier_fix(self, request_value: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request_value.project_root / observation.file
        if not target.exists():
            return None
        original = target.read_text(encoding="utf-8")
        missing = self._extract_identifier(observation.message)
        if not missing:
            return None
        candidates = [candidate for candidate in self._extract_identifiers(original) if candidate != missing]
        match = difflib.get_close_matches(missing, candidates, n=1, cutoff=0.6)
        if not match:
            return None
        candidate_name = match[0]
        pattern = re.compile(rf"\b{re.escape(missing)}\b")
        candidate_content, replacement_count = pattern.subn(candidate_name, original)
        if replacement_count == 0:
            return None
        return _build_patch(
            provider_name=self.name,
            request_value=request_value,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation=f"Replaced unknown identifier `{missing}` with the closest in-file match `{candidate_name}`.",
            evidence_paths=evidence_paths,
        )

    def _propose_endmodule_fix(self, request_value: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request_value.project_root / observation.file
        if not target.exists():
            return None
        original = target.read_text(encoding="utf-8")
        if "endmodule" in original:
            return None
        candidate_content = original.rstrip() + "\n\nendmodule\n"
        return _build_patch(
            provider_name=self.name,
            request_value=request_value,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation="Appended a missing `endmodule` at end of file.",
            evidence_paths=evidence_paths,
        )

    def _propose_end_fix(self, request_value: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request_value.project_root / observation.file
        if not target.exists():
            return None
        original = target.read_text(encoding="utf-8")
        lines = original.splitlines()
        insert_at = len(lines)
        for idx, line in enumerate(lines):
            if line.strip() == "endmodule":
                insert_at = idx
                break
        new_lines = list(lines)
        new_lines.insert(insert_at, "end")
        candidate_content = "\n".join(new_lines) + ("\n" if original.endswith("\n") else "")
        return _build_patch(
            provider_name=self.name,
            request_value=request_value,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation="Inserted a missing `end` before module termination.",
            evidence_paths=evidence_paths,
        )

    def _result(self, request_payload: dict[str, Any], proposal: PatchProposal | None, summary: str) -> RepairProviderResult:
        response_payload: dict[str, Any] = {"provider": self.name, "summary": summary}
        if proposal is None:
            response_payload["status"] = "no_patch"
        else:
            response_payload["status"] = "proposed"
            response_payload["file_path"] = proposal.file_path
            response_payload["explanation"] = proposal.explanation
        return RepairProviderResult(
            provider_name=self.name,
            request_payload=request_payload,
            response_payload=response_payload,
            proposal=proposal,
            summary=summary,
        )

    def _extract_identifier(self, message: str) -> str | None:
        patterns = [
            re.compile(r"unknown identifier\s+'?(?P<name>[A-Za-z_][A-Za-z0-9_]*)'?", re.IGNORECASE),
            re.compile(r"undeclared(?: identifier)?\s+'?(?P<name>[A-Za-z_][A-Za-z0-9_]*)'?", re.IGNORECASE),
        ]
        for pattern in patterns:
            match = pattern.search(message)
            if match:
                return match.group("name")
        return None

    def _extract_identifiers(self, text: str) -> list[str]:
        return re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)

    def _append_semicolon(self, line: str) -> str:
        stripped = line.rstrip()
        if not stripped or stripped.endswith(";"):
            return line
        if stripped.endswith(("begin", "end", ")", "(", ",", ":")):
            return line
        comment = ""
        code = stripped
        if "//" in stripped:
            code, comment = stripped.split("//", 1)
            code = code.rstrip()
            comment = "//" + comment
        if code.endswith(";"):
            return line
        suffix = "" if not comment else f" {comment}".rstrip()
        leading = line[: len(line) - len(line.lstrip())]
        return f"{leading}{code};{suffix}"


class HeuristicGenerationProvider(GenerationProvider):
    name = "heuristic"

    def generate_sva(self, request_value: GenerationRequest) -> GenerationProviderResult:
        request_payload = _build_generation_request_payload(request_value, self.name)
        return GenerationProviderResult(
            provider_name=self.name,
            request_payload=request_payload,
            response_payload={"provider": self.name, "status": "no_generation", "summary": "heuristic provider has no SVA generator"},
            candidate=None,
            summary="heuristic provider has no SVA generator",
        )

    def generate_cocotb(self, request_value: CocotbGenerationRequest) -> CocotbGenerationProviderResult:
        generation_inputs = _load_cocotb_generation_inputs(request_value)
        request_payload = _build_cocotb_generation_request_payload(request_value, self.name)
        candidate = _build_heuristic_cocotb_candidate(self.name, request_value, generation_inputs)
        return CocotbGenerationProviderResult(
            provider_name=self.name,
            request_payload=request_payload,
            response_payload={
                "provider": self.name,
                "status": "proposed",
                "file_path": candidate.file_path,
                "manifest_path": candidate.manifest_path,
                "top_module": candidate.top_module,
                "assumptions": candidate.assumptions,
            },
            candidate=candidate,
            summary="heuristic cocotb scaffold generated from DUT interface",
        )


class OpenAICompatibleRepairProvider(RepairProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _attach_provider_metadata(_build_repair_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        chat_payload = self._build_chat_payload(request_payload)
        response_payload = _invoke_openai_compatible(self.provider_name, self.config, chat_payload)
        proposal = _build_patch_from_content_payload(self.provider_name, request_value, _extract_openai_response_content(response_payload, self.provider_name))
        summary = "model-backed repair proposal generated" if proposal else "model-backed provider returned no patch"
        return RepairProviderResult(
            provider_name=self.provider_name,
            request_payload={"provider_request": request_payload, "transport_request": chat_payload},
            response_payload=response_payload,
            proposal=proposal,
            summary=summary,
        )

    def _build_chat_payload(self, provider_request: dict[str, Any]) -> dict[str, Any]:
        system_prompt = self.config.get(
            "system_prompt",
            "You are a hardware verification repair assistant. Return only valid JSON.",
        )
        return _build_openai_compatible_payload(self.config, system_prompt, provider_request)


class OpenAICompatibleGenerationProvider(GenerationProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def generate_sva(self, request_value: GenerationRequest) -> GenerationProviderResult:
        request_payload = _attach_provider_metadata(_build_generation_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        chat_payload = self._build_chat_payload(request_payload)
        response_payload = _invoke_openai_compatible(self.provider_name, self.config, chat_payload)
        candidate = _build_sva_candidate_from_content_payload(
            self.provider_name,
            request_value,
            _extract_openai_response_content(response_payload, self.provider_name),
        )
        summary = "model-backed SVA candidate generated" if candidate else "model-backed provider returned no SVA candidate"
        return GenerationProviderResult(
            provider_name=self.provider_name,
            request_payload={"provider_request": request_payload, "transport_request": chat_payload},
            response_payload=response_payload,
            candidate=candidate,
            summary=summary,
        )

    def generate_cocotb(self, request_value: CocotbGenerationRequest) -> CocotbGenerationProviderResult:
        request_payload = _attach_provider_metadata(_build_cocotb_generation_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        chat_payload = self._build_chat_payload(request_payload)
        response_payload = _invoke_openai_compatible(self.provider_name, self.config, chat_payload)
        candidate = _build_cocotb_candidate_from_content_payload(
            self.provider_name,
            request_value,
            _extract_openai_response_content(response_payload, self.provider_name),
        )
        summary = "model-backed cocotb scaffold generated" if candidate else "model-backed provider returned no cocotb scaffold"
        return CocotbGenerationProviderResult(
            provider_name=self.provider_name,
            request_payload={"provider_request": request_payload, "transport_request": chat_payload},
            response_payload=response_payload,
            candidate=candidate,
            summary=summary,
        )

    def _build_chat_payload(self, provider_request: dict[str, Any]) -> dict[str, Any]:
        system_prompt = self.config.get(
            "system_prompt",
            "You are a hardware verification assertion assistant. Return only valid JSON.",
        )
        return _build_openai_compatible_payload(self.config, system_prompt, provider_request)


class AnthropicRepairProvider(RepairProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _attach_provider_metadata(_build_repair_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        message_payload = self._build_message_payload(request_payload)
        response_payload = _invoke_anthropic(self.provider_name, self.config, message_payload)
        proposal = _build_patch_from_content_payload(self.provider_name, request_value, _extract_anthropic_response_content(response_payload, self.provider_name))
        summary = "model-backed repair proposal generated" if proposal else "model-backed provider returned no patch"
        return RepairProviderResult(
            provider_name=self.provider_name,
            request_payload={"provider_request": request_payload, "transport_request": message_payload},
            response_payload=response_payload,
            proposal=proposal,
            summary=summary,
        )

    def _build_message_payload(self, provider_request: dict[str, Any]) -> dict[str, Any]:
        system_prompt = self.config.get(
            "system_prompt",
            "You are a hardware verification repair assistant. Return only valid JSON.",
        )
        return _build_anthropic_message_payload(self.config, system_prompt, provider_request)


class AnthropicGenerationProvider(GenerationProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def generate_sva(self, request_value: GenerationRequest) -> GenerationProviderResult:
        request_payload = _attach_provider_metadata(_build_generation_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        message_payload = self._build_message_payload(request_payload)
        response_payload = _invoke_anthropic(self.provider_name, self.config, message_payload)
        candidate = _build_sva_candidate_from_content_payload(
            self.provider_name,
            request_value,
            _extract_anthropic_response_content(response_payload, self.provider_name),
        )
        summary = "model-backed SVA candidate generated" if candidate else "model-backed provider returned no SVA candidate"
        return GenerationProviderResult(
            provider_name=self.provider_name,
            request_payload={"provider_request": request_payload, "transport_request": message_payload},
            response_payload=response_payload,
            candidate=candidate,
            summary=summary,
        )

    def generate_cocotb(self, request_value: CocotbGenerationRequest) -> CocotbGenerationProviderResult:
        request_payload = _attach_provider_metadata(_build_cocotb_generation_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        message_payload = self._build_message_payload(request_payload)
        response_payload = _invoke_anthropic(self.provider_name, self.config, message_payload)
        candidate = _build_cocotb_candidate_from_content_payload(
            self.provider_name,
            request_value,
            _extract_anthropic_response_content(response_payload, self.provider_name),
        )
        summary = "model-backed cocotb scaffold generated" if candidate else "model-backed provider returned no cocotb scaffold"
        return CocotbGenerationProviderResult(
            provider_name=self.provider_name,
            request_payload={"provider_request": request_payload, "transport_request": message_payload},
            response_payload=response_payload,
            candidate=candidate,
            summary=summary,
        )

    def _build_message_payload(self, provider_request: dict[str, Any]) -> dict[str, Any]:
        system_prompt = self.config.get(
            "system_prompt",
            "You are a hardware verification assertion assistant. Return only valid JSON.",
        )
        return _build_anthropic_message_payload(self.config, system_prompt, provider_request)


class LocalCommandRepairProvider(RepairProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _attach_provider_metadata(_build_repair_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        response_payload = _invoke_local_command(self.provider_name, self.config, request_value.project_root, request_payload)
        proposal = _build_patch_from_content_payload(self.provider_name, request_value, response_payload.get("parsed", {}))
        summary = "local command repair proposal generated" if proposal else "local command provider returned no patch"
        return RepairProviderResult(
            provider_name=self.provider_name,
            request_payload=request_payload,
            response_payload=response_payload,
            proposal=proposal,
            summary=summary,
        )


class LocalCommandGenerationProvider(GenerationProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def generate_sva(self, request_value: GenerationRequest) -> GenerationProviderResult:
        request_payload = _attach_provider_metadata(_build_generation_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        response_payload = _invoke_local_command(self.provider_name, self.config, request_value.project_root, request_payload)
        candidate = _build_sva_candidate_from_content_payload(self.provider_name, request_value, response_payload.get("parsed", {}))
        summary = "local command SVA candidate generated" if candidate else "local command provider returned no SVA candidate"
        return GenerationProviderResult(
            provider_name=self.provider_name,
            request_payload=request_payload,
            response_payload=response_payload,
            candidate=candidate,
            summary=summary,
        )

    def generate_cocotb(self, request_value: CocotbGenerationRequest) -> CocotbGenerationProviderResult:
        request_payload = _attach_provider_metadata(_build_cocotb_generation_request_payload(request_value, self.provider_name), self.provider_name, self.config)
        response_payload = _invoke_local_command(self.provider_name, self.config, request_value.project_root, request_payload)
        candidate = _build_cocotb_candidate_from_content_payload(self.provider_name, request_value, response_payload.get("parsed", {}))
        summary = "local command cocotb scaffold generated" if candidate else "local command provider returned no cocotb scaffold"
        return CocotbGenerationProviderResult(
            provider_name=self.provider_name,
            request_payload=request_payload,
            response_payload=response_payload,
            candidate=candidate,
            summary=summary,
        )


class AgentRuntimeRepairProvider(RepairProvider):
    def __init__(self, provider_name: str, config: dict[str, Any], project_config: ProjectConfig, base_provider: RepairProvider) -> None:
        self.provider_name = provider_name
        self.config = config
        self.project_config = project_config
        self.base_provider = base_provider
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        runtime = LangGraphRepairRuntime(self.project_config, self.provider_name, self.config, self.base_provider)
        return runtime.run_repair(request_value)


class ProviderRegistry:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.providers = config.project.model_policy.get("providers", {})
        self.defaults = config.default_provider_by_capability()

    def get_repair(self, provider_name: str | None = None) -> RepairProvider:
        name, provider_config = self._resolve_provider("repair", provider_name)
        kind = provider_config.get("kind")
        if kind == "heuristic":
            return HeuristicRepairProvider()
        if kind == "openai_compatible":
            return OpenAICompatibleRepairProvider(name, provider_config)
        if kind == "anthropic":
            return AnthropicRepairProvider(name, provider_config)
        if kind == "local_command":
            return LocalCommandRepairProvider(name, provider_config)
        if kind == "agent_runtime":
            base_name = str(provider_config.get("base_provider"))
            base_provider = self.get_repair(base_name)
            return AgentRuntimeRepairProvider(name, provider_config, self.config, base_provider)
        raise ConfigError(f"unsupported repair provider kind: {kind}")

    def get_generation(self, provider_name: str | None = None) -> GenerationProvider:
        name, provider_config = self._resolve_provider("generation", provider_name)
        kind = provider_config.get("kind")
        if kind == "heuristic":
            return HeuristicGenerationProvider()
        if kind == "openai_compatible":
            return OpenAICompatibleGenerationProvider(name, provider_config)
        if kind == "anthropic":
            return AnthropicGenerationProvider(name, provider_config)
        if kind == "local_command":
            return LocalCommandGenerationProvider(name, provider_config)
        raise ConfigError(f"unsupported generation provider kind: {kind}")

    def statuses(self) -> list[ProviderStatus]:
        statuses: list[ProviderStatus] = []
        for provider_name in sorted(self.providers):
            provider_config = self.providers[provider_name]
            capabilities = self.config.provider_capabilities(provider_name, provider_config)
            blocked_reason = self._blocked_reason(provider_config)
            default_for = [capability for capability, name in self.defaults.items() if name == provider_name]
            model_metadata = provider_model_metadata(provider_name, provider_config, self.providers)
            statuses.append(
                ProviderStatus(
                    name=provider_name,
                    kind=str(provider_config.get("kind", "")),
                    capabilities=capabilities,
                    default_for=default_for,
                    allowed=not blocked_reason,
                    blocked_reason=blocked_reason or "",
                    network_scope=self._network_scope(provider_config),
                    auth_mode=self._auth_mode(provider_config),
                    model=model_metadata.get("model") if isinstance(model_metadata.get("model"), str) else None,
                    reasoning_level=str(model_metadata.get("reasoning_level") or "auto"),
                    reasoning_supported=bool(model_metadata.get("reasoning_supported", False)),
                    reasoning_wire_format=str(model_metadata.get("reasoning_wire_format") or "none"),
                )
            )
        return statuses

    def check(self, provider_name: str, *, live: bool = True) -> ProviderCheck:
        provider_config = self.providers.get(provider_name)
        if not isinstance(provider_config, dict):
            raise ConfigError(f"provider {provider_name} is not configured")
        capabilities = self.config.provider_capabilities(provider_name, provider_config)
        default_for = [capability for capability, name in self.defaults.items() if name == provider_name]
        blocked_reason = self._blocked_reason(provider_config)
        kind = str(provider_config.get("kind", ""))
        checks: dict[str, Any] = {
            "configured": {"status": "passed"},
            "policy": {"status": "blocked" if blocked_reason else "passed", "reason": blocked_reason or None},
        }
        if blocked_reason:
            return ProviderCheck(
                name=provider_name,
                kind=kind,
                status="blocked",
                allowed=False,
                summary=blocked_reason,
                capabilities=capabilities,
                default_for=default_for,
                checks=checks,
            )
        if not live:
            checks["transport"] = {"status": "skipped", "reason": "offline check requested"}
            return ProviderCheck(
                name=provider_name,
                kind=kind,
                status="passed",
                allowed=True,
                summary="configuration and policy checks passed",
                capabilities=capabilities,
                default_for=default_for,
                checks=checks,
            )
        if kind == "agent_runtime":
            transport = _agent_runtime_transport(provider_config)
            checks["transport"] = transport
            base_provider_name = str(provider_config.get("base_provider"))
            base_provider_config = self.providers.get(base_provider_name)
            if not isinstance(base_provider_config, dict):
                checks["base_provider_transport"] = {
                    "status": "failed",
                    "provider": base_provider_name,
                    "error": f"agent_runtime base provider is not configured: {base_provider_name}",
                }
                return ProviderCheck(
                    name=provider_name,
                    kind=kind,
                    status="failed",
                    allowed=True,
                    summary=str(checks["base_provider_transport"]["error"]),
                    capabilities=capabilities,
                    default_for=default_for,
                    checks=checks,
                )
            try:
                base_transport = _check_provider_transport(base_provider_name, base_provider_config, self.config.project_root)
            except ProviderError as exc:
                checks["base_provider_transport"] = {"status": "failed", "provider": base_provider_name, "error": str(exc)}
                return ProviderCheck(
                    name=provider_name,
                    kind=kind,
                    status="failed",
                    allowed=True,
                    summary=f"base provider {base_provider_name} check failed: {exc}",
                    capabilities=capabilities,
                    default_for=default_for,
                    checks=checks,
                )
            checks["base_provider_transport"] = {"provider": base_provider_name, **base_transport}
        else:
            try:
                transport = _check_provider_transport(provider_name, provider_config, self.config.project_root)
            except ProviderError as exc:
                checks["transport"] = {"status": "failed", "error": str(exc)}
                return ProviderCheck(
                    name=provider_name,
                    kind=kind,
                    status="failed",
                    allowed=True,
                    summary=str(exc),
                    capabilities=capabilities,
                    default_for=default_for,
                    checks=checks,
                )
            checks["transport"] = transport
        return ProviderCheck(
            name=provider_name,
            kind=kind,
            status="passed",
            allowed=True,
            summary="provider check passed",
            capabilities=capabilities,
            default_for=default_for,
            checks=checks,
        )

    def _resolve_provider(self, capability: str, provider_name: str | None) -> tuple[str, dict[str, Any]]:
        selected = provider_name or self.defaults.get(capability)
        if not selected:
            raise ConfigError(f"no default provider configured for capability {capability}")
        provider_config = self.providers.get(selected)
        if not isinstance(provider_config, dict):
            raise ConfigError(f"provider {selected} is not configured")
        capabilities = self.config.provider_capabilities(selected, provider_config)
        if capability not in capabilities:
            raise ConfigError(f"provider {selected} does not support capability {capability}")
        blocked_reason = self._blocked_reason(provider_config)
        if blocked_reason:
            raise ConfigError(f"provider {selected} is blocked by policy: {blocked_reason}")
        return selected, provider_config

    def _blocked_reason(self, provider_config: dict[str, Any]) -> str | None:
        kind = provider_config.get("kind")
        if kind == "heuristic":
            return None
        if kind in {"openai_compatible", "anthropic"}:
            scope = self._network_scope(provider_config)
            if self.config.no_egress and scope == "external_http":
                return "no_egress=true blocks remote providers"
            if self.config.model_mode == "local" and scope == "external_http":
                return "model_mode=local blocks remote providers"
            return None
        if kind == "local_command":
            if self.config.model_mode == "remote":
                return "model_mode=remote blocks local command providers"
            return None
        if kind == "agent_runtime":
            base_provider_name = provider_config.get("base_provider")
            base_provider_config = self.providers.get(str(base_provider_name))
            if not isinstance(base_provider_config, dict):
                return f"agent_runtime base provider is not configured: {base_provider_name}"
            base_blocked_reason = self._blocked_reason(base_provider_config)
            if base_blocked_reason:
                return f"base provider {base_provider_name} is blocked by policy: {base_blocked_reason}"
            return None
        return "unsupported provider kind"

    def _network_scope(self, provider_config: dict[str, Any]) -> str:
        return _provider_network_scope(provider_config, self.providers)

    def _auth_mode(self, provider_config: dict[str, Any]) -> str:
        return _provider_auth_mode(provider_config, self.providers)


def build_repair_provider(config: ProjectConfig, provider_name: str | None = None) -> RepairProvider:
    return ProviderRegistry(config).get_repair(provider_name)


def build_generation_provider(config: ProjectConfig, provider_name: str | None = None) -> GenerationProvider:
    return ProviderRegistry(config).get_generation(provider_name)


def list_provider_statuses(config: ProjectConfig) -> list[ProviderStatus]:
    return ProviderRegistry(config).statuses()


def check_provider_statuses(config: ProjectConfig, provider_name: str | None = None, *, live: bool = True) -> list[ProviderCheck]:
    registry = ProviderRegistry(config)
    provider_names = [provider_name] if provider_name else sorted(registry.providers)
    return [registry.check(name, live=live) for name in provider_names]


def _build_repair_request_payload(request_value: RepairRequest, provider_name: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": provider_name,
        "task_id": request_value.task_id,
        "workflow_type": request_value.base_run.workflow_type,
        "base_run_id": request_value.base_run.run_id,
        "files": request_value.base_run.inputs.get("files", []),
        "observations": [
            {
                "signature": observation.signature,
                "file": observation.file,
                "line": observation.line,
                "message": observation.message,
            }
            for observation in request_value.observations
        ],
        "retrieval_context": {
            "context_id": request_value.retrieval_context.context_id,
            "hits": [
                {
                    "path": hit.path,
                    "kind": hit.kind,
                    "score": hit.score,
                    "citation": hit.citation,
                    "snippet": hit.snippet,
                }
                for hit in request_value.retrieval_context.hits
            ],
        },
        "instructions": (
            "Return a JSON object with keys: status, file_path, candidate_content, explanation, evidence_paths. "
            "Use status='no_patch' if no safe minimal patch is available."
        ),
    }
    if request_value.feedback:
        payload["previous_attempts"] = request_value.feedback
        payload["instructions"] += " Use previous_attempts validation feedback to revise the next candidate."
    return payload


def _build_generation_request_payload(request_value: GenerationRequest, provider_name: str) -> dict[str, Any]:
    spec = request_value.project_root / request_value.spec_path
    rtl = request_value.project_root / request_value.rtl_path
    if not spec.exists():
        raise WorkflowInputError(f"spec file does not exist: {request_value.spec_path}")
    if not rtl.exists():
        raise WorkflowInputError(f"rtl file does not exist: {request_value.rtl_path}")
    payload: dict[str, Any] = {
        "provider": provider_name,
        "task_id": request_value.task_id,
        "workflow_type": "spec_to_sva",
        "spec": {
            "path": request_value.spec_path,
            "content": spec.read_text(encoding="utf-8"),
        },
        "rtl": {
            "path": request_value.rtl_path,
            "content": rtl.read_text(encoding="utf-8"),
        },
        "output_file": request_value.output_file,
        "conventions": request_value.conventions,
        "retrieval_context": {
            "context_id": request_value.retrieval_context.context_id,
            "hits": [
                {
                    "path": hit.path,
                    "kind": hit.kind,
                    "score": hit.score,
                    "citation": hit.citation,
                    "snippet": hit.snippet,
                }
                for hit in request_value.retrieval_context.hits
            ],
        },
        "instructions": (
            "Return a JSON object with keys: status, file_path, candidate_content, explanation, evidence_paths, properties. "
            "The properties field must be a list of objects with keys: name, summary, rationale, source_citation. "
            "Use status='no_generation' if no grounded assertion draft can be produced."
        ),
    }
    if request_value.feedback:
        payload["previous_attempts"] = request_value.feedback
        payload["instructions"] += " Use previous_attempts validator feedback to revise the next assertion draft."
    return payload


def _build_cocotb_generation_request_payload(request_value: CocotbGenerationRequest, provider_name: str) -> dict[str, Any]:
    loaded = _load_cocotb_generation_inputs(request_value)
    payload: dict[str, Any] = {
        "provider": provider_name,
        "task_id": request_value.task_id,
        "workflow_type": "dut_to_cocotb",
        "dut": {
            "path": request_value.dut_path,
            "content": loaded["dut_content"],
            "module_name": loaded["module_name"],
            "ports": [
                {
                    "name": port.name,
                    "direction": port.direction,
                    "width": port.width,
                    "role": port.role,
                }
                for port in loaded["ports"]
            ],
        },
        "spec": (
            {
                "path": request_value.spec_path,
                "content": loaded["spec_content"],
            }
            if request_value.spec_path and loaded["spec_content"] is not None
            else None
        ),
        "intent": request_value.intent,
        "output_dir": request_value.output_dir,
        "default_output_file": loaded["output_file"],
        "default_manifest_file": loaded["manifest_path"],
        "conventions": request_value.conventions,
        "inference": {
            "clock_port": loaded["clock_port"],
            "reset_port": loaded["reset_port"],
            "reset_active_low": loaded["reset_active_low"],
            "assumptions": loaded["assumptions"],
        },
        "retrieval_context": {
            "context_id": request_value.retrieval_context.context_id,
            "hits": [
                {
                    "path": hit.path,
                    "kind": hit.kind,
                    "score": hit.score,
                    "citation": hit.citation,
                    "snippet": hit.snippet,
                }
                for hit in request_value.retrieval_context.hits
            ],
        },
        "instructions": (
            "Return a JSON object with keys: status, file_path, candidate_content, explanation, evidence_paths, top_module, manifest_path, assumptions, ports. "
            "Use status='no_generation' if no grounded cocotb scaffold can be produced. "
            "The ports field must be a list of objects with keys: name, direction, width, role."
        ),
    }
    if request_value.feedback:
        payload["previous_attempts"] = request_value.feedback
        payload["instructions"] += " Use previous_attempts validator feedback to revise the next cocotb scaffold."
    return payload


def _invoke_openai_compatible(provider_name: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    auth = str(config.get("auth", "bearer"))
    url = _openai_compatible_url(config)
    headers = {
        "Content-Type": "application/json",
    }
    if auth == "bearer":
        api_key_env = config.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ProviderError(f"provider {provider_name} is missing credentials: set {api_key_env}")
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth != "none":
        raise ProviderError(f"provider {provider_name} auth must be bearer or none")
    for key, value in config.get("headers", {}).items():
        if str(key).lower() == "authorization":
            raise ProviderError(f"provider {provider_name} custom headers cannot override Authorization")
        headers[key] = value
    return _post_json(provider_name, url, payload, headers, int(config.get("timeout_seconds", 30)))


def _attach_provider_metadata(payload: dict[str, Any], provider_name: str, config: dict[str, Any]) -> dict[str, Any]:
    payload["model_selection"] = provider_model_metadata(provider_name, config)
    return payload


def _build_openai_compatible_payload(config: dict[str, Any], system_prompt: str, provider_request: dict[str, Any]) -> dict[str, Any]:
    if str(config.get("endpoint", "chat/completions")).strip("/").endswith("responses"):
        payload = {
            "model": config["model"],
            "temperature": config.get("temperature", 0),
            "instructions": system_prompt,
            "input": json.dumps(provider_request),
        }
    else:
        payload = {
            "model": config["model"],
            "temperature": config.get("temperature", 0),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(provider_request)},
            ],
        }
    return _apply_openai_reasoning(payload, config)


def _apply_openai_reasoning(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    level = str(config.get("reasoning_level", "auto"))
    if level == "auto":
        return payload
    wire_format = str(provider_model_metadata("__provider__", config).get("reasoning_wire_format") or "none")
    if wire_format == "openai_responses":
        reasoning: dict[str, Any] = {"effort": level}
        summary = config.get("reasoning_summary")
        if summary and str(summary) != "auto":
            reasoning["summary"] = summary
        payload["reasoning"] = reasoning
    elif wire_format == "openai_chat":
        payload["reasoning_effort"] = level
    return payload


def _apply_anthropic_reasoning(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    level = str(config.get("reasoning_level", "auto"))
    if level in {"auto", "none", "minimal", "xhigh"}:
        return payload
    payload["thinking"] = {"type": "adaptive"}
    payload["output_config"] = {"effort": level}
    return payload


def _build_anthropic_message_payload(config: dict[str, Any], system_prompt: str, provider_request: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "model": config["model"],
        "max_tokens": int(config.get("max_tokens", 4096)),
        "temperature": config.get("temperature", 0),
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": json.dumps(provider_request)},
        ],
    }
    return _apply_anthropic_reasoning(payload, config)


def _invoke_anthropic(provider_name: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    api_key_env = config.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ProviderError(f"provider {provider_name} is missing credentials: set {api_key_env}")
    url = _anthropic_url(config)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": str(config.get("anthropic_version", "2023-06-01")),
    }
    reserved = {"x-api-key", "anthropic-version", "content-type"}
    for key, value in config.get("headers", {}).items():
        if str(key).lower() in reserved:
            raise ProviderError(f"provider {provider_name} custom headers cannot override Anthropic transport headers")
        headers[key] = value
    return _post_json(provider_name, url, payload, headers, int(config.get("timeout_seconds", 30)))


def _post_json(provider_name: str, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    http_request = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise ProviderError(f"provider {provider_name} returned HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise ProviderError(f"provider {provider_name} request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(f"provider {provider_name} timed out after {timeout} second(s)") from exc
    except socket.timeout as exc:
        raise ProviderError(f"provider {provider_name} timed out after {timeout} second(s)") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"provider {provider_name} returned invalid JSON") from exc


def _invoke_local_command(provider_name: str, config: dict[str, Any], project_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    command = [config["command"], *config.get("args", [])]
    env = os.environ.copy()
    env.update(config.get("env", {}))
    timeout = int(config.get("timeout_seconds", 30))
    output_limit = _local_command_output_limit(config)
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProviderError(f"provider {provider_name} command was not found: {config['command']}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"provider {provider_name} timed out after {timeout} second(s)") from exc
    if result.returncode != 0:
        stderr = _bounded_text(result.stderr.strip(), output_limit)["text"]
        detail = f": {stderr}" if stderr else ""
        raise ProviderError(f"provider {provider_name} command failed with exit code {result.returncode}{detail}")
    parsed = _extract_json_object(result.stdout, provider_name)
    bounded_stdout = _bounded_text(result.stdout, output_limit)
    bounded_stderr = _bounded_text(result.stderr, output_limit)
    return {
        "provider": provider_name,
        "command": command,
        "returncode": result.returncode,
        "stdout": bounded_stdout["text"],
        "stderr": bounded_stderr["text"],
        "stdout_original_chars": bounded_stdout["original_chars"],
        "stderr_original_chars": bounded_stderr["original_chars"],
        "stdout_truncated": bounded_stdout["truncated"],
        "stderr_truncated": bounded_stderr["truncated"],
        "output_limit_chars": output_limit,
        "parsed": parsed,
    }


def _check_provider_transport(provider_name: str, config: dict[str, Any], project_root: Path) -> dict[str, Any]:
    kind = config.get("kind")
    model_metadata = provider_model_metadata(provider_name, config)
    if kind == "heuristic":
        return {"status": "passed", "mode": "builtin", **_transport_model_fields(model_metadata)}
    if kind == "local_command":
        command = str(config.get("command", ""))
        resolved = shutil.which(command) if command else None
        if resolved is None:
            raise ProviderError(f"provider {provider_name} command was not found: {command}")
        payload = _attach_provider_metadata(
            {
                "provider": provider_name,
                "workflow_type": "provider_check",
                "instructions": "Return any valid JSON object to confirm the local command provider can run.",
            },
            provider_name,
            config,
        )
        response = _invoke_local_command(provider_name, config, project_root, payload)
        parsed = response.get("parsed", {})
        return {
            "status": "passed",
            "mode": "local_command",
            "command": [command, *config.get("args", [])],
            "parsed_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else [],
            **_transport_model_fields(model_metadata),
        }
    if kind == "openai_compatible":
        api_key_env = str(config.get("api_key_env", "OPENAI_API_KEY"))
        auth = str(config.get("auth", "bearer"))
        if auth == "bearer" and not os.environ.get(api_key_env):
            raise ProviderError(f"provider {provider_name} is missing credentials: set {api_key_env}")
        payload = _build_openai_compatible_payload(config, "Return only valid JSON.", {"instructions": 'Return exactly {"status":"ok"} as JSON.'})
        response = _invoke_openai_compatible(provider_name, config, payload)
        parsed = _extract_openai_response_content(response, provider_name)
        return {
            "status": "passed",
            "mode": "openai_compatible",
            "base_url": config["base_url"],
            "endpoint": config.get("endpoint", "chat/completions"),
            "model": config["model"],
            "api_key_env": api_key_env if auth == "bearer" else None,
            "auth_mode": auth,
            "network_scope": _provider_network_scope(config, {}),
            "parsed_keys": sorted(parsed.keys()),
            **_transport_model_fields(model_metadata),
        }
    if kind == "anthropic":
        api_key_env = str(config.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not os.environ.get(api_key_env):
            raise ProviderError(f"provider {provider_name} is missing credentials: set {api_key_env}")
        payload = _build_anthropic_message_payload(config, "Return only valid JSON.", {"instructions": 'Return exactly {"status":"ok"} as JSON.'})
        response = _invoke_anthropic(provider_name, config, payload)
        parsed = _extract_anthropic_response_content(response, provider_name)
        return {
            "status": "passed",
            "mode": "anthropic",
            "base_url": config.get("base_url", "https://api.anthropic.com/v1"),
            "endpoint": config.get("endpoint", "messages"),
            "model": config["model"],
            "api_key_env": api_key_env,
            "anthropic_version": config.get("anthropic_version", "2023-06-01"),
            "network_scope": _provider_network_scope(config, {}),
            "parsed_keys": sorted(parsed.keys()),
            **_transport_model_fields(model_metadata),
        }
    if kind == "agent_runtime":
        return {**_agent_runtime_transport(config), **_transport_model_fields(model_metadata)}
    raise ProviderError(f"provider {provider_name} has unsupported kind: {kind}")


def _transport_model_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": metadata.get("model"),
        "reasoning_level": metadata.get("reasoning_level", "auto"),
        "reasoning_summary": metadata.get("reasoning_summary"),
        "model_source": metadata.get("model_source"),
        "reasoning_supported": bool(metadata.get("reasoning_supported", False)),
        "reasoning_wire_format": metadata.get("reasoning_wire_format", "none"),
        "model_warnings": metadata.get("model_warnings", []),
    }


def _agent_runtime_transport(config: dict[str, Any]) -> dict[str, Any]:
    runtime_info = runtime_capability()
    return {
        "status": "passed",
        "mode": "agent_runtime",
        "runtime": config.get("runtime", "langgraph"),
        "runtime_mode": runtime_info["runtime_mode"],
        "runtime_available": runtime_info["runtime_available"],
        "runtime_reason": runtime_info["runtime_reason"],
        "base_provider": config.get("base_provider"),
        "max_iterations": int(config.get("max_iterations", 3)),
    }


def _openai_compatible_url(config: dict[str, Any]) -> str:
    base_url = str(config["base_url"]).rstrip("/")
    endpoint = str(config.get("endpoint", "chat/completions")).strip().lstrip("/")
    return f"{base_url}/{endpoint}"


def _anthropic_url(config: dict[str, Any]) -> str:
    base_url = str(config.get("base_url", "https://api.anthropic.com/v1")).rstrip("/")
    endpoint = str(config.get("endpoint", "messages")).strip().lstrip("/")
    return f"{base_url}/{endpoint}"


def _extract_openai_response_content(response_payload: dict[str, Any], provider_name: str) -> dict[str, Any]:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return _extract_json_object(output_text, provider_name)
    output = response_payload.get("output")
    if isinstance(output, list):
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
            elif isinstance(content, str):
                text_parts.append(content)
        if text_parts:
            return _extract_json_object("\n".join(text_parts), provider_name)
    choices = response_payload.get("choices") or []
    if not choices:
        raise ProviderError(f"provider {provider_name} returned no choices")
    if not isinstance(choices[0], dict):
        raise ProviderError(f"provider {provider_name} returned malformed choices")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise ProviderError(f"provider {provider_name} returned malformed message")
    content = str(message.get("content") or "")
    if not content.strip():
        content = _extract_tool_call_arguments(message)
    return _extract_json_object(content, provider_name)


def _extract_anthropic_response_content(response_payload: dict[str, Any], provider_name: str) -> dict[str, Any]:
    content = response_payload.get("content")
    if isinstance(content, str):
        return _extract_json_object(content, provider_name)
    if not isinstance(content, list) or not content:
        raise ProviderError(f"provider {provider_name} returned no content")
    text_parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
    if not text_parts:
        raise ProviderError(f"provider {provider_name} returned no text content")
    return _extract_json_object("\n".join(text_parts), provider_name)


def _extract_tool_call_arguments(message: dict[str, Any]) -> str:
    function_call = message.get("function_call")
    if isinstance(function_call, dict) and isinstance(function_call.get("arguments"), str):
        return function_call["arguments"]
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        return ""
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        if isinstance(function, dict) and isinstance(function.get("arguments"), str):
            return function["arguments"]
    return ""


def _provider_network_scope(provider_config: dict[str, Any], providers: dict[str, Any]) -> str:
    kind = provider_config.get("kind")
    if kind == "heuristic":
        return "builtin"
    if kind == "local_command":
        return "local_process"
    if kind == "openai_compatible":
        return _http_network_scope(str(provider_config.get("base_url", "")))
    if kind == "anthropic":
        return _http_network_scope(str(provider_config.get("base_url", "https://api.anthropic.com/v1")))
    if kind == "agent_runtime":
        base_provider = providers.get(str(provider_config.get("base_provider")))
        if isinstance(base_provider, dict):
            return _provider_network_scope(base_provider, providers)
        return "unknown"
    return "unknown"


def _provider_auth_mode(provider_config: dict[str, Any], providers: dict[str, Any]) -> str:
    kind = provider_config.get("kind")
    if kind == "openai_compatible":
        return str(provider_config.get("auth", "bearer"))
    if kind == "anthropic":
        return "x-api-key"
    if kind == "agent_runtime":
        base_provider = providers.get(str(provider_config.get("base_provider")))
        if isinstance(base_provider, dict):
            return _provider_auth_mode(base_provider, providers)
        return "delegated"
    return "none"


def _http_network_scope(base_url: str) -> str:
    parsed = urlparse(base_url)
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "local_http"
    if hostname.startswith("127."):
        return "local_http"
    return "external_http"


def _build_patch_from_content_payload(provider_name: str, request_value: RepairRequest, content_payload: dict[str, Any]) -> PatchProposal | None:
    if not content_payload or content_payload.get("status") == "no_patch":
        return None
    file_path = str(content_payload.get("file_path") or request_value.base_run.inputs.get("files", [None])[0])
    if not file_path:
        raise ProviderError(f"provider {provider_name} did not return file_path")
    file_path = _normalize_project_relative_path(request_value.project_root, file_path, provider_name, "file_path")
    target = request_value.project_root / file_path
    if not target.exists():
        raise ProviderError(f"provider {provider_name} referenced missing file: {file_path}")
    candidate_content = content_payload.get("candidate_content")
    if not isinstance(candidate_content, str) or not candidate_content:
        raise ProviderError(f"provider {provider_name} did not return candidate_content")
    original = target.read_text(encoding="utf-8")
    explanation = str(content_payload.get("explanation") or "Model-backed repair proposal.")
    evidence_paths = [str(path) for path in content_payload.get("evidence_paths", [])]
    observation = next((item for item in request_value.observations if item.file == file_path), request_value.observations[0])
    return _build_patch(
        provider_name=provider_name,
        request_value=request_value,
        observation=observation,
        file_path=file_path,
        original=original,
        candidate_content=candidate_content,
        explanation=explanation,
        evidence_paths=evidence_paths,
    )


def _build_sva_candidate_from_content_payload(provider_name: str, request_value: GenerationRequest, content_payload: dict[str, Any]) -> SvaCandidate | None:
    if not content_payload or content_payload.get("status") in {"no_generation", "no_patch"}:
        return None
    candidate_content = content_payload.get("candidate_content")
    if not isinstance(candidate_content, str) or not candidate_content.strip():
        raise ProviderError(f"provider {provider_name} did not return candidate_content")
    file_path = _normalize_project_relative_path(
        request_value.project_root,
        str(content_payload.get("file_path") or request_value.output_file),
        provider_name,
        "file_path",
    )
    explanation = str(content_payload.get("explanation") or "Model-backed SVA candidate.")
    evidence_paths = [str(path) for path in content_payload.get("evidence_paths", [])]
    properties = _parse_sva_properties(content_payload.get("properties", []))
    return SvaCandidate(
        candidate_id=stable_id("sva", request_value.task_id, file_path),
        task_id=request_value.task_id,
        spec_path=request_value.spec_path,
        rtl_path=request_value.rtl_path,
        file_path=file_path,
        candidate_content=candidate_content,
        explanation=explanation,
        status="proposed",
        provider=provider_name,
        evidence_paths=evidence_paths,
        properties=properties,
    )


def _build_cocotb_candidate_from_content_payload(
    provider_name: str,
    request_value: CocotbGenerationRequest,
    content_payload: dict[str, Any],
) -> CocotbCandidate | None:
    if not content_payload or content_payload.get("status") in {"no_generation", "no_patch"}:
        return None
    loaded = _load_cocotb_generation_inputs(request_value)
    candidate_content = content_payload.get("candidate_content")
    if not isinstance(candidate_content, str) or not candidate_content.strip():
        raise ProviderError(f"provider {provider_name} did not return candidate_content")
    file_path = _normalize_project_relative_path(
        request_value.project_root,
        str(content_payload.get("file_path") or loaded["output_file"]),
        provider_name,
        "file_path",
    )
    manifest_path = _normalize_project_relative_path(
        request_value.project_root,
        str(content_payload.get("manifest_path") or loaded["manifest_path"]),
        provider_name,
        "manifest_path",
    )
    raw_assumptions = content_payload.get("assumptions", loaded["assumptions"])
    if not isinstance(raw_assumptions, list):
        raw_assumptions = loaded["assumptions"]
    assumptions = [str(item) for item in raw_assumptions if str(item).strip()]
    ports = _parse_cocotb_ports(content_payload.get("ports"), loaded["ports"])
    raw_evidence_paths = content_payload.get("evidence_paths", [])
    if not isinstance(raw_evidence_paths, list):
        raw_evidence_paths = []
    evidence_paths = [str(path) for path in raw_evidence_paths]
    top_module = str(content_payload.get("top_module") or loaded["module_name"])
    explanation = str(content_payload.get("explanation") or "Model-backed cocotb scaffold.")
    return CocotbCandidate(
        candidate_id=stable_id("cocotb", request_value.task_id, file_path),
        task_id=request_value.task_id,
        dut_path=request_value.dut_path,
        spec_path=request_value.spec_path,
        top_module=top_module,
        file_path=file_path,
        manifest_path=manifest_path,
        candidate_content=candidate_content,
        explanation=explanation,
        status="proposed",
        provider=provider_name,
        intent=request_value.intent,
        evidence_paths=evidence_paths,
        assumptions=assumptions,
        ports=ports,
    )


def _parse_sva_properties(value: Any) -> list[SvaProperty]:
    if not isinstance(value, list):
        return []
    parsed: list[SvaProperty] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        summary = str(item.get("summary") or "").strip()
        rationale = str(item.get("rationale") or "").strip()
        source_citation = str(item.get("source_citation") or "").strip()
        if not name and not summary and not rationale:
            continue
        parsed.append(
            SvaProperty(
                name=name or "unnamed_property",
                summary=summary or "No summary provided.",
                rationale=rationale or "No rationale provided.",
                source_citation=source_citation,
            )
        )
    return parsed


def _parse_cocotb_ports(value: Any, default_ports: list[CocotbPort]) -> list[CocotbPort]:
    if not isinstance(value, list):
        return list(default_ports)
    parsed: list[CocotbPort] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        direction = str(item.get("direction") or "").strip()
        if not name or not direction:
            continue
        width = item.get("width", 1)
        try:
            width_value = max(int(width), 1)
        except (TypeError, ValueError):
            width_value = 1
        parsed.append(CocotbPort(name=name, direction=direction, width=width_value, role=str(item.get("role") or "").strip()))
    return parsed or list(default_ports)


def _build_patch(
    provider_name: str,
    request_value: RepairRequest,
    observation: Observation,
    file_path: str,
    original: str,
    candidate_content: str,
    explanation: str,
    evidence_paths: list[str],
) -> PatchProposal:
    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            candidate_content.splitlines(),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=1,
            lineterm="",
        )
    )
    return PatchProposal(
        patch_id=stable_id("patch", request_value.task_id, file_path, str(observation.line)),
        task_id=request_value.task_id,
        based_on_observations=[observation.observation_id],
        file_path=file_path,
        diff=diff,
        candidate_content=candidate_content,
        explanation=explanation,
        status="proposed",
        provider=provider_name,
        evidence_paths=evidence_paths,
    )


def _extract_json_object(content: str, provider_name: str) -> dict[str, Any]:
    stripped = content.strip()
    if not stripped:
        raise ProviderError(f"provider {provider_name} returned no JSON response")
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError(f"provider {provider_name} did not return a JSON object")
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"provider {provider_name} returned malformed JSON content") from exc


def _local_command_output_limit(config: dict[str, Any]) -> int:
    raw_limit = config.get("output_limit_chars", DEFAULT_LOCAL_COMMAND_OUTPUT_LIMIT_CHARS)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LOCAL_COMMAND_OUTPUT_LIMIT_CHARS
    return max(limit, 1024)


def _bounded_text(value: str, limit: int) -> dict[str, Any]:
    original_chars = len(value)
    if original_chars <= limit:
        return {"text": value, "original_chars": original_chars, "truncated": False}
    marker = f"\n... output truncated to {limit} character(s) ..."
    keep = max(limit - len(marker), 0)
    return {
        "text": value[:keep] + marker,
        "original_chars": original_chars,
        "truncated": True,
    }


def _normalize_project_relative_path(project_root: Path, value: str, provider_name: str, field_name: str) -> str:
    normalized = Path(value)
    root = project_root.resolve()
    resolved = normalized.resolve() if normalized.is_absolute() else (root / normalized).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ProviderError(f"provider {provider_name} returned {field_name} outside the project: {value}") from exc
    if not relative.parts:
        raise ProviderError(f"provider {provider_name} returned empty {field_name}")
    return relative.as_posix()


def _load_cocotb_generation_inputs(request_value: CocotbGenerationRequest) -> dict[str, Any]:
    dut = request_value.project_root / request_value.dut_path
    if not dut.exists():
        raise WorkflowInputError(f"dut file does not exist: {request_value.dut_path}")
    dut_content = dut.read_text(encoding="utf-8")
    spec_content: str | None = None
    if request_value.spec_path:
        spec = request_value.project_root / request_value.spec_path
        if not spec.exists():
            raise WorkflowInputError(f"spec file does not exist: {request_value.spec_path}")
        spec_content = spec.read_text(encoding="utf-8")
    module_name = _extract_module_name(dut_content) or dut.stem
    conventions = _cocotb_conventions(request_value)
    ports = _extract_cocotb_ports(dut_content, conventions)
    clock_port = next((port.name for port in ports if port.role == "clock"), None)
    reset_port = next((port.name for port in ports if port.role == "reset"), None)
    reset_active_low = bool(reset_port and _is_active_low_reset_name(reset_port, conventions))
    output_dir = Path(request_value.output_dir)
    output_file = (output_dir / _render_file_template(str(conventions["test_file_template"]), module_name, dut.stem)).as_posix()
    manifest_path = (output_dir / _render_file_template(str(conventions["manifest_file_template"]), module_name, dut.stem)).as_posix()
    assumptions: list[str] = []
    if clock_port:
        assumptions.append(f"Inferred `{clock_port}` as the primary clock.")
    else:
        assumptions.append("No dedicated clock port was inferred; scaffold uses Timer-based waits.")
    if reset_port:
        polarity = "active-low" if reset_active_low else "active-high"
        assumptions.append(f"Inferred `{reset_port}` as an {polarity} reset.")
    else:
        assumptions.append("No reset port was inferred; scaffold leaves reset sequencing as a TODO.")
    if request_value.spec_path:
        assumptions.append(f"Used `{request_value.spec_path}` as supplemental behavioral context.")
    if request_value.intent.strip():
        assumptions.append("Applied user-provided test intent to shape the scaffold focus.")
    return {
        "dut_content": dut_content,
        "spec_content": spec_content,
        "module_name": module_name,
        "ports": ports,
        "clock_port": clock_port,
        "reset_port": reset_port,
        "reset_active_low": reset_active_low,
        "output_file": output_file,
        "manifest_path": manifest_path,
        "assumptions": assumptions,
    }


def _build_heuristic_cocotb_candidate(
    provider_name: str,
    request_value: CocotbGenerationRequest,
    generation_inputs: dict[str, Any],
) -> CocotbCandidate:
    ports = list(generation_inputs["ports"])
    module_name = str(generation_inputs["module_name"])
    output_file = str(generation_inputs["output_file"])
    manifest_path = str(generation_inputs["manifest_path"])
    evidence_paths = [hit.path for hit in request_value.retrieval_context.hits]
    content = _render_heuristic_cocotb(request_value, generation_inputs)
    return CocotbCandidate(
        candidate_id=stable_id("cocotb", request_value.task_id, output_file),
        task_id=request_value.task_id,
        dut_path=request_value.dut_path,
        spec_path=request_value.spec_path,
        top_module=module_name,
        file_path=output_file,
        manifest_path=manifest_path,
        candidate_content=content,
        explanation="Generated a reviewable cocotb smoke-test scaffold from DUT ports, retrieved context, and inferred reset/clock behavior.",
        status="proposed",
        provider=provider_name,
        intent=request_value.intent,
        evidence_paths=evidence_paths,
        assumptions=list(generation_inputs["assumptions"]),
        ports=ports,
    )


def _render_heuristic_cocotb(request_value: CocotbGenerationRequest, generation_inputs: dict[str, Any]) -> str:
    module_name = str(generation_inputs["module_name"])
    ports: list[CocotbPort] = list(generation_inputs["ports"])
    clock_port = generation_inputs["clock_port"]
    reset_port = generation_inputs["reset_port"]
    reset_active_low = bool(generation_inputs["reset_active_low"])
    stimulus_ports = [port for port in ports if port.direction == "input" and port.name not in {clock_port, reset_port}]
    observed_ports = [port for port in ports if port.direction in {"output", "inout"}]
    lines = [
        "import cocotb",
        "from cocotb.clock import Clock",
        "from cocotb.triggers import RisingEdge, Timer",
        "",
        "",
        "async def initialize_inputs(dut) -> None:",
    ]
    if stimulus_ports:
        for port in stimulus_ports:
            lines.append(f"    dut.{port.name}.value = {_default_signal_value(port)}")
    else:
        lines.append("    # TODO: Initialize DUT stimulus inputs once protocol intent is refined.")
        lines.append("    return")
    lines.extend(["", ""])
    if reset_port:
        inactive = "1" if reset_active_low else "0"
        active = "0" if reset_active_low else "1"
        lines.extend(
            [
                "async def apply_reset(dut) -> None:",
                f"    dut.{reset_port}.value = {active}",
            ]
        )
        if clock_port:
            lines.extend([f"    await RisingEdge(dut.{clock_port})", f"    await RisingEdge(dut.{clock_port})"])
        else:
            lines.append("    await Timer(20, units=\"ns\")")
        lines.extend(
            [
                f"    dut.{reset_port}.value = {inactive}",
            ]
        )
        if clock_port:
            lines.append(f"    await RisingEdge(dut.{clock_port})")
        else:
            lines.append("    await Timer(10, units=\"ns\")")
        lines.extend(["", ""])
    else:
        lines.extend(
            [
                "async def apply_reset(dut) -> None:",
                "    # TODO: No reset port was inferred. Add environment-specific reset handling here.",
                "    await Timer(20, units=\"ns\")",
                "",
                "",
            ]
        )
    lines.extend(
        [
            "@cocotb.test()",
            f"async def smoke_{module_name}(dut) -> None:",
            f"    \"\"\"Smoke scaffold for {module_name}.\"\"\"",
        ]
    )
    if request_value.intent.strip():
        lines.append(f"    # Intent: {request_value.intent.strip()}")
    if clock_port:
        lines.extend(
            [
                f"    clock = Clock(dut.{clock_port}, 10, units=\"ns\")",
                "    cocotb.start_soon(clock.start())",
            ]
        )
    else:
        lines.append("    # No explicit clock was inferred for this DUT.")
    lines.extend(
        [
            "    await initialize_inputs(dut)",
            "    await apply_reset(dut)",
        ]
    )
    if stimulus_ports:
        first_input = stimulus_ports[0]
        next_value = "0" if _default_signal_value(first_input) == "1" else "1"
        lines.append(f"    dut.{first_input.name}.value = {next_value}")
    if clock_port:
        lines.append(f"    await RisingEdge(dut.{clock_port})")
    else:
        lines.append("    await Timer(10, units=\"ns\")")
    if observed_ports:
        lines.append("    observed = {")
        for port in observed_ports:
            lines.append(f"        \"{port.name}\": int(dut.{port.name}.value),")
        lines.append("    }")
        lines.append("    cocotb.log.info(f\"Observed DUT outputs: {observed}\")")
    else:
        lines.append("    cocotb.log.info(\"No DUT outputs were inferred for observation.\")")
    lines.extend(
        [
            "    # TODO: Add monitors and scoreboard expectations tied to the DUT protocol.",
            "    # TODO: Extend stimulus sequences beyond this smoke path.",
            "    # TODO: Add functional coverage hooks or sampled observations once coverage goals are known.",
        ]
    )
    return "\n".join(lines) + "\n"


def _extract_module_name(content: str) -> str | None:
    match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content)
    return match.group(1) if match else None


def _extract_cocotb_ports(content: str, conventions: dict[str, Any] | None = None) -> list[CocotbPort]:
    header_match = re.search(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_]*\s*\((?P<header>.*?)\)\s*;", content, re.DOTALL)
    if not header_match:
        return []
    conventions = conventions or _default_cocotb_conventions()
    ports: list[CocotbPort] = []
    for raw_line in header_match.group("header").splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        match = re.match(
            r"(?P<direction>input|output|inout)\s+(?P<body>.+)",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        direction = match.group("direction").lower()
        body = re.sub(r"\b(logic|wire|reg|bit|signed|unsigned)\b", "", match.group("body")).strip()
        width_match = re.search(r"(\[[^\]]+\])", body)
        width = _parse_port_width(width_match.group(1) if width_match else "")
        if width_match:
            body = body.replace(width_match.group(1), "").strip()
        for name in [part.strip() for part in body.split(",") if part.strip()]:
            ports.append(CocotbPort(name=name, direction=direction, width=width, role=_infer_port_role(name, direction, conventions)))
    return ports


def _parse_port_width(width_expr: str) -> int:
    if not width_expr:
        return 1
    match = re.match(r"\[(\d+)\s*:\s*(\d+)\]", width_expr)
    if not match:
        return 1
    upper = int(match.group(1))
    lower = int(match.group(2))
    return abs(upper - lower) + 1


def _infer_port_role(name: str, direction: str, conventions: dict[str, Any] | None = None) -> str:
    lowered = name.lower()
    conventions = conventions or _default_cocotb_conventions()
    if direction == "input" and (_matches_configured_name(name, conventions.get("clock_names", [])) or re.search(r"(^|_)(clk|clock)($|_)", lowered)):
        return "clock"
    if direction == "input" and (_matches_configured_name(name, conventions.get("reset_names", [])) or re.search(r"(^|_)(rst|reset)($|_)", lowered)):
        return "reset"
    return ""


def _default_signal_value(port: CocotbPort) -> str:
    if port.width <= 1:
        return "0"
    return "0"


def _cocotb_conventions(request_value: CocotbGenerationRequest) -> dict[str, Any]:
    conventions = _default_cocotb_conventions()
    configured = request_value.conventions.get("cocotb") if isinstance(request_value.conventions, dict) else None
    if isinstance(configured, dict):
        conventions.update(configured)
    return conventions


def _default_cocotb_conventions() -> dict[str, Any]:
    return {
        "test_file_template": "test_{module}.py",
        "manifest_file_template": "{module}_cocotb_manifest.json",
        "clock_names": ["clk", "clock"],
        "reset_names": ["rst_n", "reset_n", "rst", "reset"],
        "active_low_reset_names": ["rst_n", "reset_n"],
    }


def _matches_configured_name(name: str, values: Any) -> bool:
    if not isinstance(values, list):
        return False
    lowered = name.lower()
    return lowered in {str(value).lower() for value in values}


def _is_active_low_reset_name(name: str, conventions: dict[str, Any]) -> bool:
    return _matches_configured_name(name, conventions.get("active_low_reset_names", [])) or bool(re.search(r"(_n|n$)", name, re.IGNORECASE))


def _render_file_template(template: str, module_name: str, dut_stem: str) -> str:
    return template.format(module=module_name, dut_stem=dut_stem, rtl_stem=dut_stem)
