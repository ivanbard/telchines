from __future__ import annotations

import re
from pathlib import Path

from telchines.models import Observation
from telchines.utils import stable_id

DOMAIN_PATTERNS = [
    {
        "tool_name": "uvm",
        "log_family": "uvm",
        "pattern": re.compile(
            r"(?P<severity>UVM_ERROR|UVM_FATAL|UVM_WARNING|UVM_INFO)"
            r"(?:\s+(?P<file>[^(\s]+)\((?P<line>\d+)\))?"
            r"\s+@\s+(?P<time>[^:]+):\s+(?P<component>\S+)"
            r"(?:\s+\[(?P<code>[^\]]+)\])?\s+(?P<message>.+)"
        ),
    },
    {
        "tool_name": "vivado",
        "log_family": "vendor_build",
        "pattern": re.compile(
            r"(?P<severity>ERROR|CRITICAL WARNING|WARNING):\s+\[(?P<code>[A-Za-z]+(?:\s+\d+-\d+)?)\]\s+"
            r"(?P<message>.*?)(?:\s+\[(?P<file>[^:\]]+):(?P<line>\d+)\])?$",
            re.IGNORECASE,
        ),
    },
    {
        "tool_name": "quartus",
        "log_family": "vendor_build",
        "pattern": re.compile(
            r"(?P<severity>Error|Warning)\s+\((?P<code>\d+)\):\s+"
            r"(?P<message>.*?)(?:\s+at\s+(?P<file>.+?)\((?P<line>\d+)\))?$",
            re.IGNORECASE,
        ),
    },
    {
        "tool_name": "libero",
        "log_family": "vendor_build",
        "pattern": re.compile(
            r"(?P<severity>ERROR|WARNING|Error|Warning):\s+(?P<code>[A-Za-z]+[-_ ]?\d+):\s+"
            r"(?P<message>.*?)(?:\s+File:\s*(?P<file>[^,]+),\s*Line:\s*(?P<line>\d+))?$"
        ),
    },
]

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


def normalize_signature(message: str, *, severity: str = "", tool_name: str = "", log_family: str = "", code: str = "") -> str:
    text = message.lower()
    lowered_code = code.lower()
    if log_family == "uvm":
        if "config_db" in text or "config db" in text:
            return "UVM_CONFIG_DB_ERROR"
        if "virtual interface" in text or "vif" in text:
            return "UVM_VIRTUAL_INTERFACE_ERROR"
        if "objection" in text or ("phase" in text and "timeout" in text):
            return "UVM_PHASE_OBJECTION_TIMEOUT"
        if "scoreboard" in text or "mismatch" in text:
            return "UVM_SCOREBOARD_MISMATCH"
        if "sequence" in text or "sequencer" in text:
            return "UVM_SEQUENCE_TIMEOUT" if "timeout" in text else "UVM_SEQUENCE_ERROR"
        if severity == "fatal":
            return "UVM_FATAL"
        if severity == "warning":
            return "UVM_WARNING"
        return "UVM_ERROR"
    if tool_name == "vivado":
        if "timing" in text or "timing" in lowered_code:
            return "VIVADO_TIMING_ERROR"
        if "place" in lowered_code or "placer" in text:
            return "VIVADO_PLACE_ERROR"
        if "route" in lowered_code or "route" in text:
            return "VIVADO_ROUTE_ERROR"
        if "synth" in lowered_code or "synth" in text or "synthesis" in text:
            return "VIVADO_SYNTH_ERROR"
        return "VIVADO_BUILD_WARNING" if severity == "warning" else "VIVADO_BUILD_ERROR"
    if tool_name == "quartus":
        if "fitter" in text or "fit" in text:
            return "QUARTUS_FITTER_ERROR"
        if "timing" in text or "timequest" in text:
            return "QUARTUS_TIMING_ERROR"
        if "analysis" in text or "synthesis" in text or "synth" in text:
            return "QUARTUS_SYNTH_ERROR"
        return "QUARTUS_BUILD_WARNING" if severity == "warning" else "QUARTUS_BUILD_ERROR"
    if tool_name == "libero":
        if "timing" in text or "constraint" in text:
            return "LIBERO_TIMING_ERROR"
        if "synth" in text or lowered_code.startswith("syn"):
            return "LIBERO_SYNTH_ERROR"
        if "place" in text or "route" in text:
            return "LIBERO_PLACE_ROUTE_ERROR"
        return "LIBERO_BUILD_WARNING" if severity == "warning" else "LIBERO_BUILD_ERROR"
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
        domain_observation = _parse_domain_line(run_id, line_text, default_type)
        if domain_observation is not None:
            observations.append(domain_observation)
            continue
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
                    signature=normalize_signature(message, severity=severity),
                    file=str(Path(groups["file"])) if groups.get("file") else None,
                    line=int(groups["line"]) if groups.get("line") else None,
                    message=message,
                    severity=severity,
                    metadata=_generic_metadata(line_text, groups),
                )
            )
            break
    return observations


def _parse_domain_line(run_id: str, line_text: str, default_type: str) -> Observation | None:
    for item in DOMAIN_PATTERNS:
        pattern = item["pattern"]
        match = pattern.search(line_text)
        if not match:
            continue
        groups = match.groupdict()
        severity = _normalize_severity(groups.get("severity"), groups.get("message") or line_text)
        code = (groups.get("code") or "").strip()
        message = (groups.get("message") or line_text).strip()
        if code:
            message = f"{code}: {message}"
        tool_name = str(item["tool_name"])
        log_family = str(item["log_family"])
        metadata = {
            "raw_line": line_text,
            "code": code,
            "component": (groups.get("component") or "").strip(),
            "time": (groups.get("time") or "").strip(),
        }
        return Observation(
            observation_id=stable_id("obs", run_id, line_text),
            run_id=run_id,
            type=default_type,
            signature=normalize_signature(message, severity=severity, tool_name=tool_name, log_family=log_family, code=code),
            file=str(Path(groups["file"])) if groups.get("file") else None,
            line=int(groups["line"]) if groups.get("line") else None,
            message=message,
            severity=severity,
            log_family=log_family,
            tool_name=tool_name,
            metadata={key: value for key, value in metadata.items() if value},
        )
    return None


def _normalize_severity(value: str | None, message: str) -> str:
    if value:
        lowered = value.lower().replace("uvm_", "")
        if lowered == "critical warning":
            return "warning"
        return "error" if lowered == "fatal" else lowered
    lowered_message = message.lower()
    if lowered_message.startswith("warning") or " warning" in lowered_message:
        return "warning"
    return "error"


def _generic_metadata(line_text: str, groups: dict[str, str | None]) -> dict[str, str]:
    metadata = {
        "raw_line": line_text,
        "code": (groups.get("code") or "").strip(),
        "column": (groups.get("column") or "").strip(),
        "identifier": _extract_identifier(groups.get("message") or line_text),
    }
    return {key: value for key, value in metadata.items() if value}


def _extract_identifier(message: str) -> str:
    match = re.search(
        r"(?:unknown|undeclared)\s+(?:identifier|signal|net|wire|reg)?\s*['`\"]?(?P<name>[A-Za-z_][A-Za-z0-9_$]*)",
        message,
        flags=re.IGNORECASE,
    )
    return match.group("name") if match else ""
