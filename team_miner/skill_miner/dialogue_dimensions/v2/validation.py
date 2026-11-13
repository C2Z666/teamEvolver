"""Validate coverage, provenance and versions without inventing semantic results."""

import json

from .schemas import RESULTS, Receipt


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def exact(rows, key, expected):
    actual = [row[key] for row in rows]
    require(len(actual) == len(set(actual)) and set(actual) == set(expected), "incomplete_or_duplicate_coverage")


def validate(text, task):
    raw = json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    receipt = Receipt.model_validate(raw).model_dump()
    for key in ("unit_id", "stage", "input_hash", "registry_revision"):
        require(receipt[key] == task[key], "receipt_identity_mismatch:" + key)
    result = RESULTS[task["stage"]].model_validate(receipt["result"]).model_dump()
    payload, stage = task["payload"], task["stage"]
    dimensions = {row["dimension_id"]: row["version"] for row in payload.get("dimensions", [])}
    if "dimension" in payload:
        dimensions[payload["dimension"]["dimension_id"]] = payload["dimension"]["version"]
    sentences = {row["sentence_id"]: row for row in payload.get("case", {}).get("fragments", [])
                 if "sentence_id" in row}
    spans = {row["span_ref"] for row in payload.get("spans", [])}
    if payload.get("context_contract") == "exact-evidence-v1":
        sources = {row["span_ref"]: row for row in payload.get("spans", [])}
        for evidence in payload.get("evidence", []):
            role_refs = [ref for role in ("trigger_refs", "action_refs", "response_refs") for ref in evidence[role]]
            require(set(role_refs) == set(evidence["source_span_refs"]), "evidence_reference_roles_mismatch")
            require(bool(evidence["action_refs"]), "evidence_missing_action")
            for ref in role_refs:
                source = sources.get(ref)
                version = (source.get("case_version", payload.get("source_versions", {}).get(source["case_ref"]))
                           if source else None)
                require(source is not None and source["case_ref"] == evidence["case_ref"]
                        and version == evidence["case_version"],
                        "evidence_source_missing_or_foreign")
            require(all(sources[ref]["speaker"] == "HWP" for ref in evidence["action_refs"]),
                    "evidence_action_requires_hwp")
    members = {row.get("member_ref", row.get("evidence_id")) for row in payload.get("evidence", [])}
    members.update(row["member_ref"] for row in payload.get("audit_gaps", []))
    if stage == "register":
        members.update(payload["proposal"].get("member_refs", []))

    def refs(values, allowed):
        invalid = set(values) - set(allowed)
        require(not invalid, "foreign_source_reference:" + ",".join(str(v) for v in sorted(invalid, key=str))[:500])

    def target(row):
        require(dimensions.get(row["dimension_id"]) == row["version"], "foreign_dimension_or_version")

    for issue in receipt["issues"]:
        cases = {row["case_ref"] for row in payload.get("evidence", []) if row.get("case_ref")}
        cases.update(row["case_ref"] for row in payload.get("spans", []) if row.get("case_ref"))
        refs(issue["refs"], set(sentences) | spans | members | set(dimensions) | cases)
    if stage == "extract":
        require(result["observations"] or result["empty_reason"].strip(), "missing_empty_reason")
        seen, accepted, isolated = set(), [], []
        for row in result["observations"]:
            require(row["local_ref"] not in seen, "duplicate_observation")
            seen.add(row["local_ref"])
            # Unknown references block the unit. Valid references with a wrong role isolate only this observation.
            refs(row["trigger_refs"] + row["action_refs"] + row["response_refs"], sentences)
            if any(sentences[ref]["speaker"] != "HWP" for ref in row["action_refs"]):
                isolated.append({"reason": "action_is_not_hwp", "observation": row})
                continue
            if row["business_fact_status"] == "supported_by_supplied_reference" and not payload.get("references"):
                row["business_fact_status"] = "reference_required"
            accepted.append(row)
        result.update(observations=accepted, isolated=isolated)
    elif stage == "propose":
        seen, covered = set(), set()
        for proposal in result["proposals"]:
            require(proposal["proposal_ref"] not in seen, "duplicate_proposal_ref")
            seen.add(proposal["proposal_ref"])
            refs(proposal["member_refs"], members)
            covered.update(proposal["member_refs"])
        for row in result["dispositions"]:
            refs([row["member_ref"]], members)
            covered.add(row["member_ref"])
            if row["action"] == "existing_method":
                refs([row["target_dimension_id"]], dimensions)
        require(covered == members, "missing_member_disposition")
    elif stage == "match":
        exact(result["decisions"], "evidence_id", members)
        for row in result["decisions"]:
            attach = row["action"] in {"attach", "method_variant"}
            require(bool(row["targets"]) == attach, "invalid_match_targets")
            for value in row["targets"]:
                target(value)
                refs([value["dimension_id"]], payload["allowed_candidates"][row["evidence_id"]])
    elif stage == "register":
        exact(result["comparisons"], "dimension_id", dimensions)
        for row in result["comparisons"]:
            target(row)
    elif stage == "audit":
        exact(result["assessments"], "dimension_id", dimensions)
        for row in result["assessments"]:
            target(row)
            refs(row["support_refs"] + row["counter_refs"], sentences)
            signal = row["signal"]
            context = (f":{row['dimension_id']}:applicable={row['applicable']},"
                       f"sufficient={row['sufficient']},signal={signal}")
            if row["applicable"] == "no":
                require(signal == "na", "invalid_na" + context + "; applicable=no requires signal=na")
            elif row["applicable"] == "unknown" or row["sufficient"] == "no":
                require(signal == "ne", "invalid_ne" + context +
                        "; applicable=unknown or sufficient=no requires signal=ne (unless applicable=no)")
            else:
                require(signal not in {"na", "ne"}, "invalid_applicable_signal" + context +
                        "; applicable=yes and sufficient=yes requires an evidence signal")
            if signal in {"support", "mixed"}:
                require(row["support_refs"], "missing_support")
                require(any(sentences[ref]["speaker"] == "HWP" for ref in row["support_refs"]),
                        "support_requires_hwp_behavior")
            if signal in {"mixed", "counter", "not_demonstrated"}:
                require(row["counter_refs"], "missing_counter_or_opportunity")
        for row in result["uncovered"]:
            refs(row["sentence_refs"], sentences)
    elif stage == "revise":
        changed = set()
        for row in result["changes"]:
            refs(row["source_dimension_ids"], dimensions)
            refs(row["support_refs"], spans)
            action, source, cards = row["action"], row["source_dimension_ids"], row["replacement_cards"]
            require(not changed.intersection(source), "conflicting_revision_patches")
            changed.update(source)
            require(len(source) == len(set(source)), "duplicate_patch_source")
            if action == "add":
                require(not source and cards, "invalid_add")
            elif action == "merge":
                require(len(source) >= 2 and len(cards) == 1, "invalid_merge")
            else:
                require(len(source) == 1, "invalid_revision_source")
            if action == "update":
                require(len(cards) == 1, "invalid_update")
            if action == "split":
                require(len(cards) >= 2, "invalid_split")
            if action in {"keep", "demote"}:
                require(not cards, "unexpected_replacement")
            if action == "demote":
                require(row["target_status"] is not None, "missing_demotion_status")
            for card in cards:
                refs(card["member_refs"], members)
                if action == "add":
                    require(bool(card["member_refs"]), "new_dimension_requires_source_member")
    else:
        target(result)
        scores = [row["score"] for row in result["anchors"]]
        require(len(scores) == len(set(scores)) and set(scores) <= set(payload["scale"]), "invalid_scale")
        if result["rubric_status"] == "provisional":
            require(set(scores) == set(payload["scale"]), "missing_anchor")
        for row in result["anchors"]:
            refs(row["source_refs"], spans)
            if row["basis"] in {"observed", "mixed"}:
                require(row["source_refs"], "missing_anchor_basis")
    return {**receipt, "result": result}
