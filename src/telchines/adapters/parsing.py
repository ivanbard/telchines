from __future__ import annotations

import re
from pathlib import Path

from telchines.models import Observation
from telchines.utils import stable_id

GENERIC_PATTERNS = [
    re.compile(r"ERROR:\s+(?P<file>.+?):(?P<line>\d+):\s+(?P<message>.+)"),
    re.compile(r"(?P<file>[^:\n]+):(?P<line>\d+):\s*(?P<severity>error|warning):\s*(?P<message>.+)", re.IGNORECASE),
]


def normalize_signature(message: str) -> str:
    text = message.lower()
    if "semicolon" in text:
        return "SV_PARSE_EXPECTED_SEMICOLON"
    if "syntax error" in text:
        return "SV_GENERIC_SYNTAX_ERROR"
    if "undeclared" in text or "unknown identifier" in text:
        return "SV_UNKNOWN_IDENTIFIER"
    if "assert" in text and "fail" in text:
        return "ASSERTION_FAILURE"
    compact = re.sub(r"[^a-z0-9]+", "_", text.upper()).strip("_")
    return compact[:64] or "UNKNOWN_TOOL_ERROR"


def parse_common_output(run_id: str, text: str, default_type: str = "tool_error") -> list[Observation]:
    observations: list[Observation] = []
    for line_text in text.splitlines():
        for pattern in GENERIC_PATTERNS:
            match = pattern.search(line_text)
            if not match:
                continue
            message = match.group("message").strip()
            observations.append(
                Observation(
                    observation_id=stable_id("obs", run_id, line_text),
                    run_id=run_id,
                    type=default_type,
                    signature=normalize_signature(message),
                    file=str(Path(match.group("file"))),
                    line=int(match.group("line")),
                    message=message,
                    severity=match.groupdict().get("severity", "error").lower(),
                )
            )
            break
    return observations
