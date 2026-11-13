"""Pure domain transitions applied atomically with their accepted model receipt."""

from uuid import uuid4

from ..input import case_key
from . import match_pages


def current(run):
    return [row for row in run["dimensions"].values() if row["status"] == "active"]


def link(run, evidence_id, dimension, relation, decision, method=""):
    evidence = run["evidence"].get(evidence_id)
    if evidence is None:
        return  # audit gaps have provenance, but are not a second extraction library
    item = {"evidence_id": evidence_id, "dimension_id": dimension["dimension_id"],
            "version": dimension["version"], "relation": relation, "method_text": method,
            "decision_ref": decision, "value_status": evidence["value_status"]}
    if item not in run["memberships"]:
        run["memberships"].append(item)
    run["dispositions"][evidence_id] = "attached"


def register(run, proposal, comparisons):
    """Only a complete current snapshot can authorize a new concept identity."""
    def defer():
        proposal["status"] = "deferred"
        for member in proposal["member_refs"]:
            if member in run["evidence"] and run["dispositions"].get(member) != "attached":
                run["dispositions"][member] = "deferred"

    exclude = set(proposal.get("replace_sources", []))
    dimensions = {row["dimension_id"]: row for row in current(run) if row["dimension_id"] not in exclude}
    compared = {row["dimension_id"] for row in comparisons}
    if compared != set(dimensions) or any(dimensions[row["dimension_id"]]["version"] != row["version"]
                                         for row in comparisons):
        defer()
        return
    targets = {row["dimension_id"] for row in comparisons if row["relation"] in {"same_dimension", "method_of"}}
    if len(targets) > 1 or any(row["relation"] == "uncertain" for row in comparisons):
        defer()
        return
    if targets and exclude:
        # A revision colliding with another identity needs an explicit merge proposal.
        defer()
        run["issues"].append({"reason": "revision_identity_conflict", "proposal_id": proposal["proposal_id"]})
        return
    if targets:
        dimension = dimensions[next(iter(targets))]
        relation = next(row["relation"] for row in comparisons if row["dimension_id"] == dimension["dimension_id"])
        run["metrics"]["register_reused"] += 1
    else:
        identifier = proposal.get("canonical_id") or "dim-" + uuid4().hex
        old = run["dimensions"].get(identifier)
        display_only = bool(old and {k: v for k, v in old["card"].items() if k != "name"} ==
                            {k: v for k, v in proposal["card"].items() if k != "name"})
        version = old["version"] + (0 if display_only else 1) if old else 1
        run["registry_revision"] += 1
        dimension = {"dimension_id": identifier, "version": version, "registry_revision": run["registry_revision"],
                     "card": proposal["card"], "status": "active", "previous_version": old["version"] if old else None,
                     "change_reason": proposal.get("rationale", ""), "proposal_id": proposal["proposal_id"]}
        run["dimensions"][identifier] = dimension
        run["versions"].append(dict(dimension))
        for source in (exclude - {identifier}) if not proposal.get("split_group") else ():
            run["dimensions"][source]["status"] = "superseded"
            run["lineage"].append({"source_id": source, "target_id": identifier,
                                   "registry_revision": run["registry_revision"]})
        if proposal.get("revision") and not display_only:
            run["changed"].append(identifier)
        metric = "register_updated" if old else "register_created"
        run["metrics"][metric] = run["metrics"].get(metric, 0) + 1
        relation = "same_dimension"
    for member in proposal["member_refs"]:
        link(run, member, dimension, relation, proposal["proposal_id"])
        for gap in run["gaps"]:
            if gap["member_ref"] == member:
                gap.update(status="addressed", covered_by=[dimension["dimension_id"]])
    proposal.update(status="registered", dimension_id=dimension["dimension_id"], version=dimension["version"])


def finalize_splits(run):
    """An incomplete split must not retire the only complete parent definition."""
    groups = {p["split_group"] for p in run["proposals"] if p.get("split_group")}
    for group in groups:
        proposals = [p for p in run["proposals"] if p.get("split_group") == group]
        if any(p.get("split_finalized") for p in proposals):
            continue
        complete = all(p["status"] == "registered" for p in proposals)
        run["registry_revision"] += 1
        for proposal in proposals:
            proposal["split_finalized"] = True
            child = proposal.get("dimension_id")
            if complete:
                for source in proposal["replace_sources"]:
                    run["dimensions"][source]["status"] = "superseded"
                    run["lineage"].append({"source_id": source, "target_id": child,
                                           "registry_revision": run["registry_revision"]})
            elif child:
                run["dimensions"][child]["status"] = "deferred"
                run["changed"] = [key for key in run["changed"] if key != child]
        if not complete:
            run["issues"].append({"reason": "incomplete_split_parent_retained", "split_group": group})


def consume(run, unit, receipt):
    task, result = unit["packet"], receipt["result"]
    payload, stage, identifier = task["payload"], task["stage"], task["unit_id"]
    if stage != "extract" and task["registry_revision"] != run["registry_revision"]:
        raise ValueError("stale_registry_receipt")
    run["issues"].extend({**issue, "unit_id": identifier} for issue in receipt["issues"])
    if stage == "extract":
        case = payload["case"]
        key = case_key(case)
        if key in run["extracted"]:
            raise ValueError("duplicate_successful_extract")
        run["extracted"][key] = identifier
        for row in result["observations"]:
            evidence_id = "ev-" + identifier + "-" + row["local_ref"]
            run["evidence"][evidence_id] = {**row, "evidence_id": evidence_id, "member_ref": evidence_id,
                                         "case_ref": case["case_ref"], "case_version": case["case_version"]}
            run["dispositions"][evidence_id] = "routine" if row["value_status"] == "routine" else "unresolved"
        run["issues"].extend({**issue, "unit_id": identifier} for issue in result.get("isolated", []))
    elif stage == "propose":
        proposed_members = {member for row in result["proposals"] for member in row["member_refs"]}
        for row in result["proposals"]:
            run["proposals"].append({**row, "proposal_id": identifier + ":" + row["proposal_ref"], "status": "planned"})
        for member in proposed_members:
            if run["dispositions"].get(member) != "attached":
                run["dispositions"][member] = "unresolved"
        for row in result["dispositions"]:
            if row["member_ref"] in proposed_members and row["action"] in {"routine", "defer"}:
                if row["action"] == "routine":
                    run["issues"].append({"reason": "proposal_disposition_conflict", "unit_id": identifier,
                                          "member_ref": row["member_ref"]})
                continue
            if row["action"] == "existing_method":
                link(run, row["member_ref"], run["dimensions"][row["target_dimension_id"]], "method_of", identifier)
            else:
                run["dispositions"][row["member_ref"]] = "routine" if row["action"] == "routine" else "deferred"
    elif stage == "match":
        decisions = result["decisions"]
        if payload.get("match_scope"):
            decisions = match_pages.consume_page(run, payload, decisions, identifier)
        for row in decisions:
            run["dispositions"][row["evidence_id"]] = row["action"]
            # Preserve history, but a fresh decision supersedes previous active associations.
            for membership in run["memberships"]:
                if membership["evidence_id"] == row["evidence_id"]:
                    membership["active"] = False
            for target in row["targets"]:
                link(run, row["evidence_id"], run["dimensions"][target["dimension_id"]], target["relation"], identifier,
                     target.get("method_text", row["method_text"]))
    elif stage == "register":
        if payload.get("gap_ref"):
            run.setdefault("gap_comparisons", {}).setdefault(payload["gap_ref"], []).extend(result["comparisons"])
        else:
            run["register_comparisons"].extend(result["comparisons"])
    elif stage == "audit":
        case = payload["case"]
        for row in result["assessments"]:
            run["audits"].append({**row, "case_ref": case["case_ref"], "case_version": case["case_version"],
                                  "unit_id": identifier})
        for i, row in enumerate(result["uncovered"]):
            run["gaps"].append({**row, "member_ref": identifier + f":gap-{i}",
                                "case_ref": case["case_ref"], "case_version": case["case_version"],
                                "status": "pending", "local_dimensions": [
                                    {"dimension_id": d["dimension_id"], "version": d["version"]}
                                    for d in payload["dimensions"]]})
    elif stage == "revise":
        for number, change in enumerate(result["changes"]):
            action, sources = change["action"], change["source_dimension_ids"]
            if action == "keep":
                continue
            claimed = run.setdefault("revision_patch_sources", {})
            if any(source in claimed for source in sources):
                run["issues"].append({"reason": "revision_conflict", "unit_id": identifier,
                                      "source_dimension_ids": sources})
                continue
            claimed.update({source: identifier for source in sources})
            if action == "demote":
                # Defer registry mutation until all read-only revision packets have been consumed.
                run.setdefault("pending_demotions", {})[sources[0]] = change["target_status"]
                continue
            for i, replacement in enumerate(change["replacement_cards"]):
                run["proposals"].append({**replacement, "proposal_id": f"{identifier}:change-{number}-{i}",
                    "status": "planned", "revision": True, "replace_sources": sources,
                    "split_group": f"{identifier}:change-{number}" if action == "split" else None,
                    "canonical_id": min(sources) if action in {"update", "merge"} else None,
                    "rationale": change["reason"]})
    else:
        run["rubrics"][result["dimension_id"]] = {**result, "receipt_ref": identifier}
