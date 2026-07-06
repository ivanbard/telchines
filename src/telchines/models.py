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
class AdapterDescriptor:
    name: str
    kind: str
    category: str
    validation_mode: str = "compile_only"
    binary_names: list[str] = field(default_factory=list)
    required_binaries: list[str] = field(default_factory=list)
    supported_workflows: list[str] = field(default_factory=list)
    artifact_types: list[str] = field(default_factory=list)
    available: bool = False
    enabled: bool = False
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
    tool_result: dict[str, Any] = field(default_factory=dict)
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
    log_family: str = ""
    tool_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
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
    source_domain: str = "project"
    source_label: str = "project"
    source_uri: str = ""
    ingested_at: str = ""


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
    runtime_mode: str = ""
    runtime_available: bool | None = None
    runtime_reason: str = ""
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
    attempts: list[dict[str, Any]] = field(default_factory=list)
    rejected_candidate_ids: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class CocotbPort:
    name: str
    direction: str
    width: int = 1
    role: str = ""


@dataclass(slots=True)
class CocotbCandidate:
    candidate_id: str
    task_id: str
    dut_path: str
    spec_path: str | None
    top_module: str
    file_path: str
    manifest_path: str
    candidate_content: str
    explanation: str
    status: str
    provider: str = ""
    intent: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    ports: list[CocotbPort] = field(default_factory=list)
    replay_artifacts: dict[str, str] = field(default_factory=dict)
    validation_attempts: list[ValidationAttempt] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    rejected_candidate_ids: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class CoverageItem:
    item_id: str
    module: str
    metric: str
    name: str
    hits: int = 0
    goal: int = 1
    coverage: float = 0.0
    detail: str = ""


@dataclass(slots=True)
class CoverageExclusion:
    item_id: str
    reason: str
    status: str = "active"


@dataclass(slots=True)
class CoverageReachabilityHint:
    item_id: str
    status: str
    source: str = ""
    summary: str = ""


@dataclass(slots=True)
class CoverageRecommendation:
    item_id: str
    module: str
    metric: str
    name: str
    classification: str
    priority: int
    suggested_action: str
    rationale: str
    confidence: float
    evidence_citations: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    supporting_run_ids: list[str] = field(default_factory=list)
    reachability_status: str = "unknown"


@dataclass(slots=True)
class CoveragePlan:
    plan_id: str
    task_id: str
    report_path: str
    exclusions_path: str | None
    formal_run_id: str | None
    summary: str
    focus_paths: list[str] = field(default_factory=list)
    recommendations: list[CoverageRecommendation] = field(default_factory=list)
    excluded_item_ids: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass(slots=True)
class CoverageReport:
    tool: str
    generated_at: str
    design: str
    items: list[CoverageItem] = field(default_factory=list)
    focus_paths: list[str] = field(default_factory=list)
    exclusions: list[CoverageExclusion] = field(default_factory=list)
    reachability_hints: list[CoverageReachabilityHint] = field(default_factory=list)
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
    relevance: str = "unrelated"
    evidence_status: str = "unrelated"
    score: float = 0.0
    reason: str = ""
    candidate_signals: list[str] = field(default_factory=list)


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
class FormalEvidence:
    run_id: str
    status: str
    summary: str
    property_ids: list[str] = field(default_factory=list)
    counterexample_paths: list[str] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)


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
    formal_evidence: list[FormalEvidence] = field(default_factory=list)
    waveform_evidence: list[WaveformEvidence] = field(default_factory=list)
    log_family: str = ""
    tool_name: str = ""
    domain: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
