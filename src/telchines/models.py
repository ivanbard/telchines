from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "0.1"


@dataclass(slots=True)
class VerificationProject:
    project_id: str
    name: str
    root_path: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    config_path: str = ".tel/config.json"
    tool_policy: dict[str, Any] = field(default_factory=dict)
    model_policy: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolReference:
    kind: str
    name: str
    version: str = "unknown"


@dataclass(slots=True)
class VerificationRun:
    run_id: str
    project_id: str
    commit_sha: str
    workflow_type: str
    tool: ToolReference
    inputs: dict[str, Any]
    status: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    observation_ids: list[str] = field(default_factory=list)
    summary: str = ""
    replay_command: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class Observation:
    observation_id: str
    run_id: str
    type: str
    signature: str
    file: str | None
    line: int | None
    message: str
    severity: str
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class RetrievalHit:
    path: str
    kind: str
    score: float
    start_line: int
    end_line: int
    snippet: str
    citation: str = ""
    source_hash: str = ""


@dataclass(slots=True)
class RetrievalContext:
    context_id: str
    project_id: str
    query: str
    hits: list[RetrievalHit]
    created_at: str
    mode: str = "general"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class AgentTask:
    task_id: str
    project_id: str
    workflow_type: str
    input_run_id: str | None
    status: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class ValidationAttempt:
    attempt: int
    result: str
    run_id: str | None = None
    notes: str = ""


@dataclass(slots=True)
class PatchProposal:
    patch_id: str
    task_id: str
    based_on_observations: list[str]
    file_path: str
    diff: str
    candidate_content: str
    explanation: str
    status: str
    provider: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    replay_artifacts: dict[str, str] = field(default_factory=dict)
    validation_attempts: list[ValidationAttempt] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class SvaProperty:
    name: str
    summary: str
    rationale: str
    source_citation: str = ""


@dataclass(slots=True)
class SvaCandidate:
    candidate_id: str
    task_id: str
    spec_path: str
    rtl_path: str
    file_path: str
    candidate_content: str
    explanation: str
    status: str
    provider: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    properties: list[SvaProperty] = field(default_factory=list)
    replay_artifacts: dict[str, str] = field(default_factory=dict)
    validation_attempts: list[ValidationAttempt] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class WaveformTransition:
    timestamp: int
    value: str


@dataclass(slots=True)
class WaveformSignal:
    name: str
    full_name: str
    scope: str = ""
    width: int = 1
    identifier: str = ""


@dataclass(slots=True)
class WaveformSample:
    signal_name: str
    full_name: str
    transitions: list[WaveformTransition] = field(default_factory=list)


@dataclass(slots=True)
class WaveformSummary:
    waveform_id: str
    project_id: str
    source_path: str
    source_hash: str
    format: str
    timescale: str
    top_scopes: list[str] = field(default_factory=list)
    signals: list[WaveformSignal] = field(default_factory=list)
    sampled_signals: list[WaveformSample] = field(default_factory=list)
    external_tool: str = ""
    notes: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class WaveformEvidence:
    waveform_id: str
    source_path: str
    matched_signals: list[str] = field(default_factory=list)
    excerpt: str = ""


@dataclass(slots=True)
class BenchmarkCase:
    benchmark_id: str
    task_type: str
    title: str
    fixture_root: str
    config: dict[str, Any]
    scoring: dict[str, Any]
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class SimilarRunMatch:
    run_id: str
    score: float
    summary: str


@dataclass(slots=True)
class FailureCluster:
    cluster_id: str
    signature: str
    count: int
    files: list[str]
    summary: str
    observation_ids: list[str]
    likely_cause: str = ""
    suggested_action: str = ""
    evidence_context_id: str = ""
    evidence_hits: list[RetrievalHit] = field(default_factory=list)
    similar_runs: list[SimilarRunMatch] = field(default_factory=list)
    waveform_evidence: list[WaveformEvidence] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
