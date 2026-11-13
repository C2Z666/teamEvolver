"""Combine bounded candidate pages without letting the final page overwrite prior matches."""

from ..input import canonical, sha


def split_payload(payload, registry_revision, stage):
    evidence_id = payload["evidence"][0]["evidence_id"]
    scope = payload.get("match_scope") or {
        "key": "match-" + sha(canonical([stage, registry_revision, evidence_id,
                                        payload["allowed_candidates"][evidence_id]]))[:24],
        "evidence_id": evidence_id, "dimension_ids": payload["allowed_candidates"][evidence_id]}
    rows = payload["dimensions"]
    middle = len(rows) // 2
    return [{**payload, "dimensions": batch, "match_scope": scope,
             "allowed_candidates": {evidence_id: [d["dimension_id"] for d in batch]}}
            for batch in (rows[:middle], rows[middle:])]


def consume_page(run, payload, decisions, unit_id):
    scope = payload["match_scope"]
    state = run.setdefault("match_scopes", {}).setdefault(scope["key"], {**scope, "pages": {}, "status": "pending"})
    page_ids = payload["allowed_candidates"][scope["evidence_id"]]
    state["pages"][unit_id] = {"dimension_ids": page_ids, "decision": decisions[0]}
    covered = {identifier for page in state["pages"].values() for identifier in page["dimension_ids"]}
    if covered != set(scope["dimension_ids"]):
        run["dispositions"][scope["evidence_id"]] = "deferred"
        for membership in run["memberships"]:
            if membership["evidence_id"] == scope["evidence_id"]:
                membership["active"] = False
        return []
    rows = [page["decision"] for page in state["pages"].values()]
    targets = [{**target, "method_text": row["method_text"]} for row in rows for target in row["targets"]]
    if any(row["action"] == "uncertain" for row in rows):
        action, targets = "uncertain", []
    elif targets:
        action = "attach"
    elif all(row["action"] == "routine" for row in rows):
        action = "routine"
    else:
        action = "unmatched"
    state.update(status="complete", action=action)
    return [{"evidence_id": scope["evidence_id"], "action": action, "targets": targets,
             "method_text": "", "reason": "已收齐全部召回候选页面；逐页语义判断保留在 match_scopes"}]
