"""Bind complete receipts to frozen units and isolate invalid source claims."""

import copy
import json

from .schemas import Evidence, Fact, Inference, Issue, Receipt, Uncertainty

EMPTY = {"text": "", "sentence_refs": [], "metadata_refs": []}


def validate(text, task):
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != set(Receipt.model_fields):
        raise ValueError("invalid_receipt_envelope")
    for key in ("unit_id", "stage", "input_hash", "case_ref", "case_version"):
        if value[key] != task[key]:
            raise ValueError("receipt_identity_mismatch:" + key)
    if value["schema_version"] != "taikang.evidence-result.v1":
        raise ValueError("invalid_receipt_schema")
    if value["status"] not in {"analyzed", "analyzed_with_issues", "needs_split"}:
        raise ValueError("invalid_receipt_status")
    result = value["result"]
    if (not isinstance(result, dict) or set(result) != {"evidence", "empty_reason"}
            or not isinstance(result["evidence"], list) or not isinstance(result["empty_reason"], str)):
        raise ValueError("invalid_receipt_result")
    if bool(result["evidence"]) == bool(result["empty_reason"].strip()):
        raise ValueError("invalid_empty_reason")
    if not isinstance(value["issues"], list):
        raise ValueError("invalid_issues")
    fragments = task["payload"]["fragments"]
    visible = {f["sentence_id"]: f for f in fragments if "sentence_id" in f}
    order = {key: i for i, key in enumerate(visible)}
    metadata = task["payload"]["metadata"]
    owned = task["payload"]["scope"]["owned_sentence_ids"]

    def refs(items, role=None):
        return (isinstance(items, list) and all(isinstance(s, str) and s in visible
                and (role is None or visible[s]["speaker"] == role) for s in items))

    for issue in value["issues"]:
        Issue.model_validate(issue)
        if not refs(issue["refs"]):
            raise ValueError("invalid_issue_reference")
    if value["status"] == "needs_split":
        if result["evidence"] or not any(i["reason"] in {"input_capacity", "output_capacity"} for i in value["issues"]):
            raise ValueError("invalid_split_receipt")
        raise ValueError("capacity_exceeded")
    accepted, isolated, seen = [], [], set()

    def fact(value, trigger_before=None):
        Fact.model_validate(value)
        if not refs(value["sentence_refs"]) or any(not metadata.get(k) for k in value["metadata_refs"]):
            raise ValueError("bad_fact_refs")
        if bool(value["text"].strip()) != bool(value["sentence_refs"] or value["metadata_refs"]):
            raise ValueError("unsupported_fact")
        if trigger_before is not None and any(order[s] > trigger_before for s in value["sentence_refs"]):
            raise ValueError("trigger_after_action")
        return value

    for raw in result["evidence"]:
        row = copy.deepcopy(raw)
        local = row.get("local_ref", "") if isinstance(row, dict) else ""
        try:
            if not isinstance(row, dict) or set(row) - set(Evidence.model_fields):
                raise ValueError("unknown_evidence_fields")
            actions = row.get("action_refs")
            if not actions or not refs(actions, "HWP") or not str(row.get("behavior", "")).strip():
                raise ValueError("invalid_action")
            if actions != sorted(set(actions), key=order.get) or actions[0] not in owned:
                raise ValueError("invalid_owner_or_order")
            if not local or local in seen:
                raise ValueError("duplicate_local_ref")
            seen.add(local)
        except (ValueError, TypeError) as exc:
            isolated.append({"local_ref": local, "reason": "isolated_evidence", "detail": str(exc), "raw": raw})
            continue
        for key in ("scenario", "trigger"):
            try:
                row[key] = fact(row.get(key, EMPTY), order[actions[0]] if key == "trigger" else None)
            except (ValueError, TypeError):
                row[key] = dict(EMPTY)
                isolated.append({"local_ref": local, "reason": "isolated_field", "field": key})
        for key, schema in (("observed_conditions", Fact), ("suggested_applicability", Inference),
                            ("uncertainties", Uncertainty)):
            good = []
            rows = row.get(key, [])
            if not isinstance(rows, list):
                rows = [rows]
            for item in rows:
                try:
                    schema.model_validate(item)
                    if key == "observed_conditions":
                        fact(item)
                        if not item["text"].strip():
                            raise ValueError("empty_condition")
                    elif key == "suggested_applicability":
                        if not item["text"].strip() or not item["basis_refs"] or not refs(item["basis_refs"]):
                            raise ValueError("invalid_inference")
                    elif not item["code"].strip() or not item["detail"].strip() or not refs(item["sentence_refs"]):
                        raise ValueError("invalid_uncertainty")
                    good.append(item)
                except (ValueError, TypeError):
                    isolated.append({"local_ref": local, "reason": "isolated_field", "field": key, "raw": item})
            row[key] = good
        if not refs(row.get("response_refs", []), "C"):
            isolated.append({"local_ref": local, "reason": "isolated_field", "field": "response_refs"})
            row["response_refs"] = []
        row.setdefault("response_refs", [])
        try:
            accepted.append(Evidence.model_validate(row).model_dump())
        except ValueError as exc:
            isolated.append({"local_ref": local, "reason": "isolated_evidence", "detail": str(exc), "raw": raw})
    return {**value, "result": {"evidence": accepted, "empty_reason": result["empty_reason"]},
            "quarantined": isolated, "raw_evidence_count": len(result["evidence"])}
