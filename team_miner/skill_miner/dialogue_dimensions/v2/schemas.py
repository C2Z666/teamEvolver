"""Stage schemas shared by model assignments and strict host acceptance."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Card(Model):
    name: str = Field(min_length=1)
    question: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    applicability: str = Field(min_length=1)
    boundaries: list[str]
    indicators: list[str]
    scope: Literal["core", "scene", "undetermined"]


class Observation(Model):
    local_ref: str = Field(min_length=1)
    behavior: str = Field(min_length=1)
    evaluation_question: str = Field(min_length=1)
    trigger_refs: list[str]
    action_refs: list[str] = Field(min_length=1)
    response_refs: list[str]
    value_status: Literal["support_candidate", "mixed", "routine", "uncertain"]
    limits: list[str]
    business_fact_status: Literal["not_involved", "reference_required", "supported_by_supplied_reference"]


class Extract(Model):
    observations: list[Observation]
    empty_reason: str


class Proposal(Model):
    proposal_ref: str
    card: Card
    member_refs: list[str] = Field(min_length=1)
    rationale: str


class Disposition(Model):
    member_ref: str
    action: Literal["routine", "defer", "existing_method"]
    reason: str
    target_dimension_id: str | None = None


class Propose(Model):
    proposals: list[Proposal] = Field(max_length=3)
    dispositions: list[Disposition]


class Target(Model):
    dimension_id: str
    version: int
    relation: Literal["same_dimension", "method_of"]


class Decision(Model):
    evidence_id: str
    action: Literal["attach", "method_variant", "unmatched", "routine", "uncertain"]
    targets: list[Target]
    method_text: str
    reason: str


class Match(Model):
    decisions: list[Decision]


class Comparison(Model):
    dimension_id: str
    version: int
    relation: Literal["same_dimension", "method_of", "different", "uncertain"]
    reason: str


class Register(Model):
    comparisons: list[Comparison]


class Assessment(Model):
    dimension_id: str
    version: int
    applicable: Literal["yes", "no", "unknown"]
    sufficient: Literal["yes", "no"]
    signal: Literal["support", "mixed", "counter", "not_demonstrated", "na", "ne"]
    support_refs: list[str]
    counter_refs: list[str]
    reason: str = Field(min_length=1)


class Gap(Model):
    sentence_refs: list[str] = Field(min_length=1)
    issue: str
    suggested_question: str


class Audit(Model):
    assessments: list[Assessment]
    uncovered: list[Gap]


class Replacement(Model):
    proposal_ref: str
    card: Card
    member_refs: list[str]


class Change(Model):
    action: Literal["keep", "update", "merge", "split", "add", "demote"]
    source_dimension_ids: list[str]
    reason: str
    support_refs: list[str]
    replacement_cards: list[Replacement] = Field(default_factory=list)
    target_status: Literal["evidence_only", "needs_review"] | None = None


class Revise(Model):
    changes: list[Change]


class Applicability(Model):
    when: str
    na: str
    ne: str


class Anchor(Model):
    score: int
    description: str
    basis: Literal["observed", "designed", "mixed"]
    source_refs: list[str]


class Rubric(Model):
    dimension_id: str
    version: int
    rubric_status: Literal["provisional", "pending"]
    applicability: Applicability
    anchors: list[Anchor]
    method_rules: list[str]
    calibration_notes: list[str] = Field(min_length=1)


class Issue(Model):
    reason: str
    refs: list[str]
    detail: str


class Receipt(Model):
    schema_version: Literal["taikang.dimension-result.v2"]
    unit_id: str
    stage: Literal["extract", "propose", "match", "register", "audit", "revise", "rubric"]
    input_hash: str
    registry_revision: int | None
    status: Literal["analyzed", "analyzed_with_issues"]
    result: dict
    issues: list[Issue]


RESULTS = {"extract": Extract, "propose": Propose, "match": Match, "register": Register,
           "audit": Audit, "revise": Revise, "rubric": Rubric}
