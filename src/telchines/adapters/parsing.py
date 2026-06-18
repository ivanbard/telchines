from __future__ import annotations

import re
from pathlib import Path

from telchines.models import Observation
from telchines.utils import stable_id

GENERIC_PATTERNS = [
    re.compile(
        r"%(?P<severity>Error|Warning|Fatal)(?:-(?P<code>[A-Za-z0-9_]+))?:\s+"
        r"(?P<file>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s+(?P<message>.+)"
    ),
    re.compile(
        r"(?P<severity>error|warning|fatal):\s+"
        r"(?P<file>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s+(?P<message>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<file>[^:\n]+):(?P<line>\d+):(?P<column>\d+):\s*"
        r"(?P<severity>error|warning|fatal):\s*(?P<message>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<file>[^:\n]+):(?P<line>\d+):(?P<column>\d+):\s*"
        r"(?P<message>(?:syntax|parse)\s+error.+|.+(?:syntax|parse)\s+error.+)",
        re.IGNORECASE,
    ),
    re.compile(r"ERROR:\s+(?P<file>.+?):(?P<line>\d+):\s+(?P<message>.+)"),
    re.compile(r"(?P<file>[^:\n]+):(?P<line>\d+):\s*(?P<severity>error|warning):\s*(?P<message>.+)", re.IGNORECASE),
    re.compile(r"(?P<file>[^:\n]+):(?P<line>\d+):\s*(?P<message>syntax error.*|parse error.*)", re.IGNORECASE),
    re.compile(
        r"Assert failed in\s+(?P<property>[A-Za-z_][A-Za-z0-9_$]*)"
        r"(?:\s+at\s+(?P<file>[^:\n]+):(?P<line>\d+))?:?\s*(?P<message>.*)",
        re.IGNORECASE,
    ),
]


def normalize_signature(message: str) -> str:
    text = message.lower()
    if "semicolon" in text:
        return "SV_PARSE_EXPECTED_SEMICOLON"
    if "endmodule" in text:
        return "SV_EXPECTED_ENDMODULE"
    if ("missing end" in text) or ("expecting 'end'" in text) or ("expected end before" in text):
        return "SV_EXPECTED_END"
    if "syntax error" in text:
        return "SV_GENERIC_SYNTAX_ERROR"
    if "parse error" in text:
        return "SV_GENERIC_PARSE_ERROR"
    if "width" in text and ("warning" in text or "mismatch" in text or "expects" in text):
        return "SV_WIDTH_WARNING"
    if "malformed statement" in text:
        return "SV_MALFORMED_STATEMENT"
    if "undeclared" in text or "unknown identifier" in text:
        return "SV_UNKNOWN_IDENTIFIER"
    if "cannot open" in text or "no such file" in text:
        return "FILE_NOT_FOUND"
    if "timeout" in text:
        return "SIM_TIMEOUT"
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
            groups = match.groupdict()
            message = (groups.get("message") or line_text).strip()
            if groups.get("code"):
                message = f"{groups['code']}: {message}"
            severity = _normalize_severity(groups.get("severity"), message)
            if groups.get("property") and "assert failed" not in message.lower():
                message = f"Assert failed in {groups['property']}: {message}".rstrip(": ")
            observations.append(
                Observation(
                    observation_id=stable_id("obs", run_id, line_text),
                    run_id=run_id,
                    type=default_type,
                    signature=normalize_signature(message),
                    file=str(Path(groups["file"])) if groups.get("file") else None,
                    line=int(groups["line"]) if groups.get("line") else None,
                    message=message,
                    severity=severity,
                )
            )
            break
    return observations


def _normalize_severity(value: str | None, message: str) -> str:
    if value:
        lowered = value.lower()
        return "error" if lowered == "fatal" else lowered
    lowered_message = message.lower()
    if lowered_message.startswith("warning") or " warning" in lowered_message:
        return "warning"
    return "error"
