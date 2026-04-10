from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urljoin

from telchines.config import ProjectConfig
from telchines.errors import ConfigError, ProviderError
from telchines.models import Observation, PatchProposal, RetrievalContext, VerificationRun
from telchines.utils import stable_id


@dataclass(slots=True)
class RepairRequest:
    task_id: str
    project_root: Path
    base_run: VerificationRun
    observations: list[Observation]
    retrieval_context: RetrievalContext


@dataclass(slots=True)
class RepairProviderResult:
    provider_name: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    proposal: PatchProposal | None
    summary: str


@dataclass(slots=True)
class ProviderStatus:
    name: str
    kind: str
    capabilities: list[str]
    default_for: list[str]
    allowed: bool
    blocked_reason: str = ""


class RepairProvider:
    name = "base"
    is_remote = False

    def propose_patch(self, request: RepairRequest) -> RepairProviderResult:
        raise NotImplementedError


class HeuristicRepairProvider(RepairProvider):
    name = "heuristic"

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _build_provider_request_payload(request_value, self.name)
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


class OpenAICompatibleRepairProvider(RepairProvider):
    is_remote = True

    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _build_provider_request_payload(request_value, self.provider_name)
        chat_payload = self._build_chat_payload(request_payload)
        response_payload = self._invoke(chat_payload)
        proposal = _build_patch_from_content_payload(self.provider_name, request_value, self._extract_response_content(response_payload))
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
        return {
            "model": self.config["model"],
            "temperature": self.config.get("temperature", 0),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(provider_request)},
            ],
        }

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key_env = self.config.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ProviderError(f"provider {self.provider_name} is missing credentials: set {api_key_env}")
        base_url = self.config["base_url"].rstrip("/") + "/"
        endpoint = self.config.get("endpoint", "chat/completions")
        url = urljoin(base_url, endpoint)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        for key, value in self.config.get("headers", {}).items():
            headers[key] = value
        http_request = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        timeout = int(self.config.get("timeout_seconds", 30))
        try:
            with request.urlopen(http_request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raise ProviderError(f"provider {self.provider_name} returned HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise ProviderError(f"provider {self.provider_name} request failed: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider {self.provider_name} returned invalid JSON") from exc

    def _extract_response_content(self, response_payload: dict[str, Any]) -> dict[str, Any]:
        choices = response_payload.get("choices") or []
        if not choices:
            return {}
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        return _extract_json_object(content, self.provider_name)


class LocalCommandRepairProvider(RepairProvider):
    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = _build_provider_request_payload(request_value, self.provider_name)
        response_payload = self._invoke(request_value, request_payload)
        proposal = _build_patch_from_content_payload(self.provider_name, request_value, response_payload.get("parsed", {}))
        summary = "local command repair proposal generated" if proposal else "local command provider returned no patch"
        return RepairProviderResult(
            provider_name=self.provider_name,
            request_payload=request_payload,
            response_payload=response_payload,
            proposal=proposal,
            summary=summary,
        )

    def _invoke(self, request_value: RepairRequest, payload: dict[str, Any]) -> dict[str, Any]:
        command = [self.config["command"], *self.config.get("args", [])]
        env = os.environ.copy()
        env.update(self.config.get("env", {}))
        timeout = int(self.config.get("timeout_seconds", 30))
        try:
            result = subprocess.run(
                command,
                cwd=request_value.project_root,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"provider {self.provider_name} command was not found: {self.config['command']}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"provider {self.provider_name} timed out after {timeout} second(s)") from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise ProviderError(f"provider {self.provider_name} command failed with exit code {result.returncode}{detail}")
        parsed = _extract_json_object(result.stdout, self.provider_name)
        return {
            "provider": self.provider_name,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "parsed": parsed,
        }


class RepairProviderRegistry:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.providers = config.project.model_policy.get("providers", {})
        self.defaults = config.default_provider_by_capability()

    def get(self, capability: str) -> RepairProvider:
        provider_name = self.defaults.get(capability)
        if not provider_name:
            raise ConfigError(f"no default provider configured for capability {capability}")
        provider_config = self.providers.get(provider_name)
        if not isinstance(provider_config, dict):
            raise ConfigError(f"provider {provider_name} is not configured")
        capabilities = self.config.provider_capabilities(provider_name, provider_config)
        if capability not in capabilities:
            raise ConfigError(f"provider {provider_name} does not support capability {capability}")
        blocked_reason = self._blocked_reason(provider_config)
        if blocked_reason:
            raise ConfigError(f"provider {provider_name} is blocked by policy: {blocked_reason}")
        kind = provider_config.get("kind")
        if kind == "heuristic":
            return HeuristicRepairProvider()
        if kind == "openai_compatible":
            return OpenAICompatibleRepairProvider(provider_name, provider_config)
        if kind == "local_command":
            return LocalCommandRepairProvider(provider_name, provider_config)
        raise ConfigError(f"unsupported repair provider kind: {kind}")

    def statuses(self) -> list[ProviderStatus]:
        statuses: list[ProviderStatus] = []
        for provider_name in sorted(self.providers):
            provider_config = self.providers[provider_name]
            capabilities = self.config.provider_capabilities(provider_name, provider_config)
            blocked_reason = self._blocked_reason(provider_config)
            default_for = [capability for capability, name in self.defaults.items() if name == provider_name]
            statuses.append(
                ProviderStatus(
                    name=provider_name,
                    kind=str(provider_config.get("kind", "")),
                    capabilities=capabilities,
                    default_for=default_for,
                    allowed=not blocked_reason,
                    blocked_reason=blocked_reason or "",
                )
            )
        return statuses

    def _blocked_reason(self, provider_config: dict[str, Any]) -> str | None:
        kind = provider_config.get("kind")
        if kind == "heuristic":
            return None
        if kind == "openai_compatible":
            if self.config.no_egress:
                return "no_egress=true blocks remote providers"
            if self.config.model_mode == "local":
                return "model_mode=local blocks remote providers"
            return None
        if kind == "local_command":
            if self.config.model_mode == "remote":
                return "model_mode=remote blocks local command providers"
            return None
        return "unsupported provider kind"


def build_repair_provider(config: ProjectConfig) -> RepairProvider:
    return RepairProviderRegistry(config).get("repair")


def list_provider_statuses(config: ProjectConfig) -> list[ProviderStatus]:
    return RepairProviderRegistry(config).statuses()


def _build_provider_request_payload(request_value: RepairRequest, provider_name: str) -> dict[str, Any]:
    return {
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


def _build_patch_from_content_payload(provider_name: str, request_value: RepairRequest, content_payload: dict[str, Any]) -> PatchProposal | None:
    if not content_payload or content_payload.get("status") == "no_patch":
        return None
    file_path = str(content_payload.get("file_path") or request_value.base_run.inputs.get("files", [None])[0])
    if not file_path:
        raise ProviderError(f"provider {provider_name} did not return file_path")
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
