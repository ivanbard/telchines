from __future__ import annotations

from pathlib import Path

from telchines.models import Observation, PatchProposal
from telchines.utils import stable_id


class RepairProvider:
    def propose_patch(self, task_id: str, project_root: Path, observations: list[Observation]) -> PatchProposal | None:
        raise NotImplementedError


class HeuristicRepairProvider(RepairProvider):
    def propose_patch(self, task_id: str, project_root: Path, observations: list[Observation]) -> PatchProposal | None:
        for observation in observations:
            if observation.file is None or observation.line is None:
                continue
            if "SEMICOLON" not in observation.signature:
                continue
            target = project_root / observation.file
            if not target.exists():
                continue
            original = target.read_text(encoding="utf-8")
            lines = original.splitlines()
            index = observation.line - 1
            if index < 0 or index >= len(lines):
                continue
            updated_line = self._append_semicolon(lines[index])
            if updated_line == lines[index]:
                continue
            new_lines = list(lines)
            new_lines[index] = updated_line
            candidate_content = "\n".join(new_lines) + ("\n" if original.endswith("\n") else "")
            import difflib

            diff = "\n".join(
                difflib.unified_diff(
                    original.splitlines(),
                    candidate_content.splitlines(),
                    fromfile=f"a/{observation.file}",
                    tofile=f"b/{observation.file}",
                    lineterm="",
                )
            )
            return PatchProposal(
                patch_id=stable_id("patch", task_id, observation.file, str(observation.line)),
                task_id=task_id,
                based_on_observations=[observation.observation_id],
                file_path=observation.file,
                diff=diff,
                candidate_content=candidate_content,
                explanation="Added a missing semicolon at the reported error location.",
                status="proposed",
            )
        return None

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
