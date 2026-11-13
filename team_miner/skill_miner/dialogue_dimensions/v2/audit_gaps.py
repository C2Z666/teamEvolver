"""Reconcile packet-local uncovered findings against the complete frozen registry."""

from ..input import canonical, case_key, sha


def consolidate(run):
    groups = {}
    previous = run.get("gap_comparisons", {})
    comparisons = {}
    for row in run["gaps"]:
        key = sha(canonical([case_key(row), sorted(row["sentence_refs"]), row["suggested_question"].strip()]))
        if key not in groups:
            groups[key] = {**row, "member_ref": "gap-" + key[:24], "status": "pending",
                           "origins": [], "checked_registry_revision": run["registry_revision"]}
            if row.get("checked_registry_revision") == run["registry_revision"]:
                groups[key]["status"] = row.get("status", "pending")
                comparisons[groups[key]["member_ref"]] = previous.get(row["member_ref"], [])
        groups[key]["origins"].extend(row.get("origins", [row["member_ref"]]))
    run["gaps"] = list(groups.values())
    run["gap_comparisons"] = comparisons


def proposal(gap):
    return {"proposal_ref": gap["member_ref"], "member_refs": [gap["member_ref"]],
            "rationale": "局部审计提出的疑似遗漏，须核对全目录；此卡只供比较，不是已登记维度。",
            "card": {"name": gap["suggested_question"], "question": gap["suggested_question"],
                     "definition": gap["issue"], "applicability": "当前案例中给出的表现机会",
                     "boundaries": ["仅核对该评价目标是否已有维度覆盖，不把局部目录遗漏当成全局遗漏"],
                     "indicators": [gap["suggested_question"]], "scope": "undetermined"}}


def finalize(run):
    expected = {d["dimension_id"]: d["version"] for d in run["dimensions"].values() if d["status"] == "active"}
    for gap in run["gaps"]:
        rows = run.get("gap_comparisons", {}).get(gap["member_ref"], [])
        actual = {r["dimension_id"]: r["version"] for r in rows}
        if actual != expected or len(rows) != len(expected):
            gap["status"] = "review_incomplete"
            continue
        targets = [r["dimension_id"] for r in rows if r["relation"] in {"same_dimension", "method_of"}]
        gap["covered_by"] = targets
        if targets:
            gap["status"] = "covered"
        elif any(r["relation"] == "uncertain" for r in rows):
            gap["status"] = "needs_review"
        else:
            gap["status"] = "new_candidate"
