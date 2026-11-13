"""Formal model and offline delivery contracts, shared with OV."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Fact(Strict):
    text: str
    sentence_refs: list[str]
    metadata_refs: list[str]


class Inference(Strict):
    text: str
    basis_refs: list[str]


class Uncertainty(Strict):
    code: str
    detail: str
    sentence_refs: list[str]


class Evidence(Strict):
    local_ref: str = Field(pattern=r"^e[1-9][0-9]*$")
    behavior: str = Field(min_length=1)
    action_refs: list[str] = Field(min_length=1)
    scenario: Fact
    trigger: Fact
    observed_conditions: list[Fact]
    suggested_applicability: list[Inference]
    response_refs: list[str]
    uncertainties: list[Uncertainty]


class Issue(Strict):
    reason: str = Field(min_length=1)
    refs: list[str]
    detail: str = Field(min_length=1)


class Result(Strict):
    evidence: list[Evidence]
    empty_reason: str


class Receipt(Strict):
    schema_version: Literal["taikang.evidence-result.v1"]
    unit_id: str
    stage: Literal["extract"]
    input_hash: str
    case_ref: str
    case_version: str
    status: Literal["analyzed", "analyzed_with_issues", "needs_split"]
    result: Result
    issues: list[Issue]


class Scope(Strict):
    kind: Literal["full_case", "segment"]
    owned_sentence_ids: list[str]
    context_sentence_ids: list[str]
    segment_index: int
    segment_count: int


class Fragment(Strict):
    text: str
    sentence_id: str | None = None
    speaker: Literal["HWP", "C", "unknown"] | None = None


class Payload(Strict):
    metadata: dict[str, str]
    fragments: list[Fragment]
    scope: Scope


class Task(Strict):
    schema_version: Literal["taikang.evidence-task.v1"]
    execution_protocol: Literal["te-dialogue-evidence.v1"]
    unit_id: str
    stage: Literal["extract"]
    input_hash: str
    output_path: str
    case_ref: str
    case_version: str
    payload: Payload


class ExportEvidence(Evidence):
    schema_version: Literal["taikang.evidence.v1"]
    evidence_id: str
    case_ref: str
    case_version: str
    job_id: str
    unit_id: str
    receipt_hash: str
    skill_hash: str
    dataset_hash: str
    source_scope: Scope


class Sentence(Strict):
    sentence_id: str
    turn_index: int
    speaker: Literal["HWP", "C", "unknown"]
    char_start: int
    char_end: int
    boundary_kind: Literal["sentence", "utterance"]


class SourceCase(Strict):
    schema_version: Literal["taikang.dialogue-case.v1"]
    case_ref: str
    case_version: str
    course: str
    chapter: str
    knowledge_point: str
    scene: str
    dialogue_text: str
    sentence_index: list[Sentence]


class CoveredUnit(Strict):
    unit_id: str
    status: str
    owned_sentence_ids: list[str]
    reason: str


class Coverage(Strict):
    case_ref: str
    case_version: str
    outcome: Literal["complete", "zero_evidence", "partial", "failed", "cancelled"]
    evidence_count: int
    quarantined_count: int
    units: list[CoveredUnit]


class FileEntry(Strict):
    path: str
    sha256: str
    bytes: int
    rows: int | None = None


class Manifest(Strict):
    schema_version: Literal["taikang.evidence-delivery.v1"]
    delivery_status: Literal["complete", "partial", "failed", "cancelled"]
    job_id: str
    dataset_hash: str
    skill_hash: str
    execution_hash: str
    renderer_version: int
    config_revision: int | None
    options: dict[str, int]
    case_count: int
    evidence_count: int
    raw_evidence_count: int
    quarantined_evidence_count: int
    files: list[FileEntry]
