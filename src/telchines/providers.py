from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
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


class RepairProvider:
    name = "base"
    is_remote = False

    def propose_patch(self, request: RepairRequest) -> RepairProviderResult:
        raise NotImplementedError


class HeuristicRepairProvider(RepairProvider):
    name = "heuristic"

    def propose_patch(self, request: RepairRequest) -> RepairProviderResult:
        evidence_paths = [hit.path for hit in request.retrieval_context.hits]
        request_payload = self._request_payload(request, evidence_paths)
        for observation in request.observations:
            if observation.file is None or observation.line is None:
                continue
            if "SEMICOLON" in observation.signature:
                proposal = self._propose_semicolon_fix(request, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic semicolon repair")
            if observation.signature == "SV_UNKNOWN_IDENTIFIER":
                proposal = self._propose_identifier_fix(request, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic identifier repair")
            if observation.signature == "SV_EXPECTED_ENDMODULE":
                proposal = self._propose_endmodule_fix(request, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic endmodule repair")
            if observation.signature == "SV_EXPECTED_END":
                proposal = self._propose_end_fix(request, observation, evidence_paths)
                return self._result(request_payload, proposal, "heuristic end repair")
        return self._result(request_payload, None, "heuristic provider found no supported repair")

    def _propose_semicolon_fix(self, request: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request.project_root / observation.file
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
        return self._build_patch(
            request=request,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation="Added a missing semicolon at the reported error location.",
            evidence_paths=evidence_paths,
        )

    def _propose_identifier_fix(self, request: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request.project_root / observation.file
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
        return self._build_patch(
            request=request,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation=f"Replaced unknown identifier `{missing}` with the closest in-file match `{candidate_name}`.",
            evidence_paths=evidence_paths,
        )

    def _propose_endmodule_fix(self, request: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request.project_root / observation.file
        if not target.exists():
            return None
        original = target.read_text(encoding="utf-8")
        if "endmodule" in original:
            return None
        candidate_content = original.rstrip() + "\n\nendmodule\n"
        return self._build_patch(
            request=request,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation="Appended a missing `endmodule` at end of file.",
            evidence_paths=evidence_paths,
        )

    def _propose_end_fix(self, request: RepairRequest, observation: Observation, evidence_paths: list[str]) -> PatchProposal | None:
        target = request.project_root / observation.file
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
        return self._build_patch(
            request=request,
            observation=observation,
            file_path=observation.file,
            original=original,
            candidate_content=candidate_content,
            explanation="Inserted a missing `end` before module termination.",
            evidence_paths=evidence_paths,
        )

    def _build_patch(
        self,
        request: RepairRequest,
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
            patch_id=stable_id("patch", request.task_id, file_path, str(observation.line)),
            task_id=request.task_id,
            based_on_observations=[observation.observation_id],
            file_path=file_path,
            diff=diff,
            candidate_content=candidate_content,
            explanation=explanation,
            status="proposed",
            provider="heuristic",
            evidence_paths=evidence_paths,
        )

    def _request_payload(self, request: RepairRequest, evidence_paths: list[str]) -> dict[str, Any]:
        return {
            "task_id": request.task_id,
            "base_run_id": request.base_run.run_id,
            "observations": [
                {
                    "signature": observation.signature,
                    "file": observation.file,
                    "line": observation.line,
                    "message": observation.message,
                }
                for observation in request.observations
            ],
            "context_id": request.retrieval_context.context_id,
            "evidence_paths": evidence_paths,
        }

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
    name = "openai_compatible"
    is_remote = True

    def __init__(self, provider_name: str, config: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.config = config
        self.name = provider_name

    def propose_patch(self, request_value: RepairRequest) -> RepairProviderResult:
        request_payload = self._build_chat_payload(request_value)
        response_payload = self._invoke(request_payload)
        proposal = self._build_patch_from_response(request_value, response_payload)
        summary = "model-backed repair proposal generated" if proposal else "model-backed provider returned no patch"
        return RepairProviderResult(
            provider_name=self.provider_name,
            request_payload=request_payload,
            response_payload=response_payload,
            proposal=proposal,
            summary=summary,
        )

    def _build_chat_payload(self, request_value: RepairRequest) -> dict[str, Any]:
        observation_summary = [
            {
                "signature": observation.signature,
                "file": observation.file,
                "line": observation.line,
                "message": observation.message,
            }
            for observation in request_value.observations
        ]
        evidence = [
            {
                "path": hit.path,
                "kind": hit.kind,
                "score": hit.score,
                "snippet": hit.snippet,
            }
            for hit in request_value.retrieval_context.hits
        ]
        user_payload = {
            "task_id": request_value.task_id,
            "base_run_id": request_value.base_run.run_id,
            "files": request_value.base_run.inputs.get("files", []),
            "observations": observation_summary,
            "evidence": evidence,
            "instructions": (
                "Return a JSON object with keys: status, file_path, candidate_content, explanation, evidence_paths. "
                "Use status='no_patch' if no safe minimal patch is available."
            ),
        }
        system_prompt = self.config.get(
            "system_prompt",
            "You are a hardware verification repair assistant. Return only valid JSON.",
        )
        return {
            "model": self.config["model"],
            "temperature": self.config.get("temperature", 0),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
        }

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key_env = self.config.get("api_key_env", "OPENAI_API_KEY")
        import os

        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ProviderError(f"environment variable {api_key_env} is required for provider {self.provider_name}")
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

    def _build_patch_from_response(self, request_value: RepairRequest, response_payload: dict[str, Any]) -> PatchProposal | None:
        content = self._extract_content(response_payload)
        if not content:
            return None
        content_payload = self._extract_json_object(content)
        if content_payload.get("status") == "no_patch":
            return None
        file_path = str(content_payload.get("file_path") or request_value.base_run.inputs.get("files", [None])[0])
        if not file_path:
            raise ProviderError(f"provider {self.provider_name} did not return file_path")
        target = request_value.project_root / file_path
        if not target.exists():
            raise ProviderError(f"provider {self.provider_name} referenced missing file: {file_path}")
        candidate_content = content_payload.get("candidate_content")
        if not isinstance(candidate_content, str) or not candidate_content:
            raise ProviderError(f"provider {self.provider_name} did not return candidate_content")
        original = target.read_text(encoding="utf-8")
        explanation = str(content_payload.get("explanation") or "Model-backed repair proposal.")
        evidence_paths = [str(path) for path in content_payload.get("evidence_paths", [])]
        observation = next((observation for observation in request_value.observations if observation.file == file_path), request_value.observations[0])
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
            provider=self.provider_name,
            evidence_paths=evidence_paths,
        )

    def _extract_content(self, response_payload: dict[str, Any]) -> str:
        choices = response_payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def _extract_json_object(self, content: str) -> dict[str, Any]:
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1:
            raise ProviderError(f"provider {self.provider_name} did not return a JSON object")
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider {self.provider_name} returned malformed JSON content") from exc


def build_repair_provider(config: ProjectConfig) -> RepairProvider:
    model_policy = config.project.model_policy
    provider_name = model_policy.get("repair_provider", "heuristic")
    providers = model_policy.get("providers", {})
    provider_config = providers.get(provider_name)
    if not isinstance(provider_config, dict):
        raise ConfigError(f"repair provider {provider_name} is not configured")
    kind = provider_config.get("kind")
    if kind == "heuristic":
        return HeuristicRepairProvider()
    if kind == "openai_compatible":
        if config.no_egress or config.model_mode == "local":
            raise ConfigError(f"repair provider {provider_name} requires network access but the project is configured for local-only use")
        return OpenAICompatibleRepairProvider(provider_name, provider_config)
    raise ConfigError(f"unsupported repair provider kind: {kind}")
