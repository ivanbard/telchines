from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from telchines.errors import ProviderError
from telchines.models import Observation, RetrievalContext, ToolReference, VerificationRun
from telchines.providers import (
    LocalCommandRepairProvider,
    RepairRequest,
    _build_anthropic_message_payload,
    _build_openai_compatible_payload,
    _build_patch_from_content_payload,
    _extract_anthropic_response_content,
    _extract_json_object,
    _extract_openai_response_content,
    _invoke_local_command,
    _openai_compatible_url,
)


REASONING_LEVELS = st.sampled_from(["none", "minimal", "low", "medium", "high", "xhigh"])


def _repair_request(project_root: Path) -> RepairRequest:
    observation = Observation(
        observation_id="obs_1",
        run_id="run_1",
        type="compile",
        signature="SV_SEMICOLON",
        file="rtl/broken_counter.sv",
        line=8,
        message="missing semicolon",
        severity="error",
    )
    return RepairRequest(
        task_id="task_1",
        project_root=project_root,
        base_run=VerificationRun(
            run_id="run_1",
            project_id="proj_1",
            commit_sha="workspace",
            workflow_type="compile_repair",
            tool=ToolReference(kind="linter", name="fixture"),
            inputs={"files": ["rtl/broken_counter.sv"]},
            status="failed",
            started_at="2026-04-13T00:00:00+00:00",
        ),
        observations=[observation],
        retrieval_context=RetrievalContext(
            context_id="ctx_1",
            project_id="proj_1",
            query="missing semicolon",
            hits=[],
            created_at="2026-04-13T00:00:00+00:00",
            mode="repair",
        ),
    )


def test_extract_json_object_accepts_fenced_and_noisy_content() -> None:
    assert _extract_json_object('notes\n```json\n{"status":"ok"}\n```', "mock") == {"status": "ok"}
    assert _extract_json_object('prefix {"status":"ok"} suffix', "mock") == {"status": "ok"}


def test_extract_json_object_rejects_empty_or_malformed_content() -> None:
    with pytest.raises(ProviderError, match="no JSON"):
        _extract_json_object("", "mock")
    with pytest.raises(ProviderError, match="malformed JSON"):
        _extract_json_object('{"status": }', "mock")


def test_openai_compatible_url_preserves_base_path_prefix() -> None:
    assert (
        _openai_compatible_url(
            {
                "base_url": "http://127.0.0.1:11434/proxy/v1/",
                "endpoint": "/chat/completions",
            }
        )
        == "http://127.0.0.1:11434/proxy/v1/chat/completions"
    )


def test_local_command_provider_bounds_persisted_output(sample_project: Path) -> None:
    script = sample_project / "tools" / "noisy_provider.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "sys.stderr.write('e' * 1100)",
                "sys.stdout.write('x' * 1200 + json.dumps({'status': 'ok'}))",
            ]
        ),
        encoding="utf-8",
    )

    result = _invoke_local_command(
        "noisy",
        {
            "command": sys.executable,
            "args": [str(script)],
            "timeout_seconds": 5,
            "output_limit_chars": 64,
        },
        sample_project,
        {"workflow_type": "provider_check"},
    )

    assert result["parsed"] == {"status": "ok"}
    assert result["output_limit_chars"] == 1024
    assert result["stdout_original_chars"] > result["output_limit_chars"]
    assert result["stderr_original_chars"] > result["output_limit_chars"]
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert "truncated" in result["stdout"]
    assert "truncated" in result["stderr"]


def test_extract_openai_response_content_rejects_empty_choices() -> None:
    with pytest.raises(ProviderError, match="no choices"):
        _extract_openai_response_content({"choices": []}, "mock")


def test_extract_openai_response_content_accepts_tool_call_arguments() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "return_telchines_json",
                                "arguments": '{"status":"ok","file_path":"rtl/demo.sv"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    assert _extract_openai_response_content(payload, "mock") == {"status": "ok", "file_path": "rtl/demo.sv"}


def test_extract_openai_response_content_accepts_legacy_function_call_arguments() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "function_call": {
                        "name": "return_telchines_json",
                        "arguments": '{"status":"ok","workflow_type":"provider_check"}',
                    },
                }
            }
        ]
    }
    assert _extract_openai_response_content(payload, "mock") == {"status": "ok", "workflow_type": "provider_check"}


def test_extract_openai_response_content_accepts_responses_output_text() -> None:
    assert _extract_openai_response_content({"output_text": '{"status":"ok"}'}, "mock") == {"status": "ok"}


def test_openai_responses_reasoning_payload_uses_canonical_level() -> None:
    payload = _build_openai_compatible_payload(
        {"model": "gpt-test", "endpoint": "responses", "reasoning_level": "high", "reasoning_summary": "concise"},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )
    assert payload["input"] == '{"workflow_type": "provider_check"}'
    assert payload["reasoning"] == {"effort": "high", "summary": "concise"}
    assert "messages" not in payload


def test_openai_chat_reasoning_effort_is_gated_by_provider_support() -> None:
    unsupported = _build_openai_compatible_payload(
        {"model": "chat-test", "reasoning_level": "medium"},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )
    supported = _build_openai_compatible_payload(
        {"model": "chat-test", "reasoning_level": "medium", "supports_reasoning_effort": True},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )
    assert "reasoning_effort" not in unsupported
    assert supported["reasoning_effort"] == "medium"


def test_openai_chat_reasoning_effort_can_be_enabled_by_explicit_wire_format() -> None:
    payload = _build_openai_compatible_payload(
        {"model": "chat-test", "reasoning_level": "medium", "reasoning_wire_format": "openai_chat"},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )

    assert payload["reasoning_effort"] == "medium"


@settings(max_examples=20)
@given(level=REASONING_LEVELS)
def test_openai_auto_omits_reasoning_fields_for_all_non_auto_levels_when_unsupported(level: str) -> None:
    payload = _build_openai_compatible_payload(
        {"model": "chat-test", "reasoning_level": level},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )

    assert "reasoning" not in payload
    assert "reasoning_effort" not in payload


def test_openai_auto_reasoning_level_omits_provider_specific_fields() -> None:
    responses_payload = _build_openai_compatible_payload(
        {"model": "gpt-test", "endpoint": "responses", "reasoning_level": "auto"},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )
    chat_payload = _build_openai_compatible_payload(
        {"model": "chat-test", "reasoning_level": "auto", "supports_reasoning_effort": True},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )

    assert "reasoning" not in responses_payload
    assert "reasoning_effort" not in chat_payload


def test_anthropic_reasoning_payload_uses_adaptive_thinking() -> None:
    payload = _build_anthropic_message_payload(
        {"model": "claude-test", "reasoning_level": "low"},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "low"}


@settings(max_examples=20)
@given(level=st.sampled_from(["auto", "none", "minimal", "xhigh"]))
def test_anthropic_unsupported_reasoning_levels_are_omitted(level: str) -> None:
    payload = _build_anthropic_message_payload(
        {"model": "claude-test", "reasoning_level": level},
        "Return JSON.",
        {"workflow_type": "provider_check"},
    )

    assert "thinking" not in payload
    assert "output_config" not in payload


def test_local_command_repair_payload_includes_model_selection(sample_project: Path) -> None:
    script = sample_project / "tools" / "metadata_provider.py"
    script.write_text(
        "\n".join(
            [
                "import json, sys",
                "payload = json.loads(sys.stdin.read())",
                "selection = payload['model_selection']",
                "assert selection['model'] == 'wrapper-model'",
                "assert selection['reasoning_level'] == 'high'",
                "print(json.dumps({'status': 'no_patch', 'seen_selection': selection}))",
            ]
        ),
        encoding="utf-8",
    )
    provider = LocalCommandRepairProvider(
        "local-test",
        {
            "kind": "local_command",
            "command": sys.executable,
            "args": [str(script)],
            "model": "wrapper-model",
            "reasoning_level": "high",
            "timeout_seconds": 5,
        },
    )

    result = provider.propose_patch(_repair_request(sample_project))

    assert result.request_payload["model_selection"]["model"] == "wrapper-model"
    assert result.request_payload["model_selection"]["reasoning_level"] == "high"
    assert result.response_payload["parsed"]["seen_selection"]["model"] == "wrapper-model"


def test_extract_anthropic_response_content_accepts_text_blocks() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": "notes\n```json\n{\"status\":\"ok\",\"workflow_type\":\"provider_check\"}\n```",
            }
        ]
    }
    assert _extract_anthropic_response_content(payload, "mock") == {"status": "ok", "workflow_type": "provider_check"}


def test_extract_anthropic_response_content_rejects_missing_text() -> None:
    with pytest.raises(ProviderError, match="no text content"):
        _extract_anthropic_response_content({"content": [{"type": "thinking"}]}, "mock")


def test_repair_provider_rejects_paths_outside_project(sample_project: Path) -> None:
    request = _repair_request(sample_project)
    original = (sample_project / "rtl" / "broken_counter.sv").read_text(encoding="utf-8")
    with pytest.raises(ProviderError, match="outside the project"):
        _build_patch_from_content_payload(
            "mock",
            request,
            {
                "status": "proposed",
                "file_path": "../outside.sv",
                "candidate_content": original,
            },
        )


def test_repair_provider_rejects_symlink_path_outside_project(sample_project: Path, work_root: Path) -> None:
    outside = work_root / "outside.sv"
    outside.write_text("module outside; endmodule\n", encoding="utf-8")
    link = sample_project / "rtl" / "linked_outside.sv"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    request = _repair_request(sample_project)
    with pytest.raises(ProviderError, match="outside the project"):
        _build_patch_from_content_payload(
            "mock",
            request,
            {
                "status": "proposed",
                "file_path": "rtl/linked_outside.sv",
                "candidate_content": "module outside; endmodule\n",
            },
        )
