from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from telchines.models import Observation, PatchProposal, RetrievalContext, VerificationRun
from telchines.utils import stable_id


@dataclass(slots=True)
class RepairRequest:
    task_id: str
    project_root: Path
    base_run: VerificationRun
    observations: list[Observation]
    retrieval_context: RetrievalContext


class RepairProvider:
    def propose_patch(self, request: RepairRequest) -> PatchProposal | None:
        raise NotImplementedError


class HeuristicRepairProvider(RepairProvider):
    def propose_patch(self, request: RepairRequest) -> PatchProposal | None:
        evidence_paths = [hit.path for hit in request.retrieval_context.hits]
        for observation in request.observations:
            if observation.file is None or observation.line is None:
                continue
            if "SEMICOLON" in observation.signature:
                return self._propose_semicolon_fix(request, observation, evidence_paths)
            if observation.signature == "SV_UNKNOWN_IDENTIFIER":
                return self._propose_identifier_fix(request, observation, evidence_paths)
            if observation.signature == "SV_EXPECTED_ENDMODULE":
                return self._propose_endmodule_fix(request, observation, evidence_paths)
            if observation.signature == "SV_EXPECTED_END":
                return self._propose_end_fix(request, observation, evidence_paths)
        return None

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
