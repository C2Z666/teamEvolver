"""Identity-bound field patches; valid Evidence and unrelated fields are immutable."""

import copy
import json
from typing import Literal

from ..dialogue_dimensions.input import canonical, sha
from .schemas import Fact, Strict
from .validation import validate

FIELDS = {"scenario", "trigger", "response_refs"}
IDENTITY = ("unit_id", "stage", "input_hash", "case_ref", "case_version")


class FieldUpdate(Strict):
    local_ref: str
    field: Literal["scenario", "trigger", "response_refs"]
    value: Fact | list[str]


class FieldPatch(Strict):
    schema_version: Literal["taikang.evidence-field-patch.v1"]
    unit_id: str
    stage: Literal["extract"]
    input_hash: str
    case_ref: str
    case_version: str
    base_receipt_hash: str
    repair_hash: str
    patches: list[FieldUpdate]


def can_patch(candidate):
    issues = candidate["receipt"]["quarantined"]
    return bool(candidate.get("raw_text") and issues and all(
        i["reason"] == "isolated_field" and i.get("field") in FIELDS for i in issues))


def request(unit):
    """Send only defective Evidence rows plus their frozen source context."""
    base = unit["fallback"]
    if not can_patch(base) or sha(base["raw_text"]) != base["receipt_hash"]:
        raise ValueError("invalid_patch_base")
    original = json.loads(base["raw_text"])
    allowed = sorted({(i["local_ref"], i["field"]) for i in base["receipt"]["quarantined"]})
    rows = {r["local_ref"]: r for r in original["result"]["evidence"]}
    value = {**copy.deepcopy(unit["packet"]), "mode": "field_patch", "base_receipt_hash": base["receipt_hash"],
             "targets": [{"local_ref": ref, "field": field} for ref, field in allowed],
             "original_evidence": [rows[ref] for ref in sorted({ref for ref, _ in allowed})],
             "validation_errors": [{k: i.get(k) for k in ("local_ref", "field", "detail")}
                                   for i in base["receipt"]["quarantined"]]}
    value["repair_hash"] = sha(canonical(value))
    return value


def merge(text, unit):
    expected = request(unit)
    patch = FieldPatch.model_validate_json(text).model_dump()
    for key in (*IDENTITY, "base_receipt_hash", "repair_hash"):
        if patch[key] != expected[key]:
            raise ValueError("patch_identity_mismatch:" + key)
    allowed = {(t["local_ref"], t["field"]) for t in expected["targets"]}
    seen = set()
    merged = json.loads(unit["fallback"]["raw_text"])
    rows = {r["local_ref"]: r for r in merged["result"]["evidence"]}
    for item in patch["patches"]:
        target = (item["local_ref"], item["field"])
        if target not in allowed or target in seen:
            raise ValueError("patch_changes_unassigned_field")
        seen.add(target)
        rows[item["local_ref"]][item["field"]] = item["value"]
    if seen != allowed:
        raise ValueError("patch_missing_fields")
    raw = canonical(merged)
    receipt = validate(raw, unit["packet"])
    if receipt["quarantined"]:
        raise ValueError("patch_still_invalid")
    return raw, receipt
