"""One durable job drives bounded discovery, exhaustive audit and final rubrics."""

import json

from ..input import canonical, case_key, parse_model, sha
from ..orchestration import frozen_read, update
from ..transport import descendant
from . import PROTOCOL, audit_gaps, evidence_context, executor, match_pages
from .compiler import sections
from .input_view import DEFAULTS, model_view, packet
from .matching import neighbors, recall
from .registry import consume, current, finalize_splits, register
from .repository import Repository


async def prepare(engine, job):
    sections(job["compiler_body_snapshot"])
    capabilities = await engine._knowledge_request(job["account"], "GET", "/api/v1/compile/capabilities", timeout=10)
    if not capabilities.get("can_create"):
        raise ValueError("S2 requires OV Compile permission; check /api/v1/compile/capabilities")
    embedding = await engine._knowledge_request(job["account"], "POST", "/api/v1/compile/embeddings", timeout=30,
        json={"target_uri": job["input_dir"], "texts": []})
    if not embedding.get("model"):
        raise ValueError("S2 requires OV /api/v1/compile/embeddings with model fingerprint")
    raw = await frozen_read(engine, job, job["dataset_manifest_ref"])
    if sha(raw) != job["dataset_manifest_sha256"]:
        raise ValueError("dataset_manifest_changed")
    manifest = json.loads(raw)
    expected = manifest.pop("dataset_sha256", "")
    if expected != sha(canonical(manifest)) or manifest.get("schema_version") != "taikang.dialogue-dataset.v1":
        raise ValueError("invalid_dataset_manifest")
    root = job["dataset_manifest_ref"].rsplit("/", 1)[0]
    cases, issues = {}, []
    for page in manifest["pages"]:
        entry = page["manifest"]
        text = await frozen_read(engine, job, descendant(root, entry["path"]))
        if sha(text) != entry["sha256"]:
            raise ValueError("dataset_page_changed")
        for line in text.splitlines():
            item = json.loads(line)
            uri = descendant(root, item["path"])
            if not uri.startswith(job["input_dir"].rstrip("/") + "/"):
                raise ValueError("case_outside_source")
            content = await frozen_read(engine, job, uri)
            if sha(content) != item["sha256"]:
                raise ValueError("frozen_case_changed")
            case = parse_model(content)
            if any(case[key] != item[key] for key in ("case_ref", "case_version")) or case_key(case) in cases:
                raise ValueError("duplicate_or_changed_case")
            cases[case_key(case)] = case
        entry = page["issues"]
        text = await frozen_read(engine, job, descendant(root, entry["path"]))
        if sha(text) != entry["sha256"]:
            raise ValueError("dataset_issues_changed")
        issues.extend(json.loads(line) for line in text.splitlines())
    if len(cases) != manifest["case_count"]:
        raise ValueError("dataset_case_count_mismatch")
    return {"execution_protocol": PROTOCOL, "skill_hash": sha(job["compiler_body_snapshot"]),
            "dataset_hash": expected, "embedding_model": embedding["model"],
            "options": job.get("s2_options", dict(DEFAULTS)), "stage": "extract", "planned": False,
            "queue": [], "planned_packets": {}, "active": None, "unit_count": 0, "registry_revision": 0, "cases": cases,
            "extracted": {}, "evidence": {}, "dispositions": {}, "proposals": [], "dimensions": {},
            "versions": [], "memberships": [], "audits": [], "rubrics": {}, "gaps": [], "issues": issues,
            "lineage": [], "changed": [], "novelty_round": 0,
            "metrics": {"compiles": 0, "accepted": 0, "retries": 0, "register_reused": 0, "register_created": 0,
                        "register_updated": 0,
                        "stage_units": {}, "token_usage": "unavailable", "request_context_tokens": "unavailable",
                        "rate_limit_control": "conservative_compile_admission_no_hard_tpm", "peak_packet_chars": 0}}


async def enqueue(repo, run, stage, payload, split_key=None):
    identity = sha(canonical([run["stage"], run["novelty_round"], run.get("register_proposal"), payload]))
    if identity in run["planned_packets"]:
        return
    if run["unit_count"] >= run["options"]["max_model_units"]:
        run["issues"].append({"reason": "model_unit_budget_exhausted", "stage": stage})
        return

    def make_packet(content):
        return packet(stage, content, None if stage == "extract" else run["registry_revision"],
                      run["unit_count"] + 1, run["options"])

    omitted = []
    try:
        content = evidence_context.ground(run, payload) if stage != "extract" else payload
        try:
            value = make_packet(content)
        except ValueError as exc:
            if str(exc) != "capacity_pending" or stage not in {"revise", "rubric"}:
                raise
            content, omitted = evidence_context.select_to_budget(run, stage, payload, make_packet)
            value = make_packet(content)
    except ValueError as exc:
        if str(exc) != "capacity_pending":
            raise
        if split_key and len(payload[split_key]) > 1:
            middle = len(payload[split_key]) // 2
            for rows in (payload[split_key][:middle], payload[split_key][middle:]):
                child = {**payload, split_key: rows}
                if stage == "match":
                    child["allowed_candidates"] = {r["evidence_id"]: payload["allowed_candidates"][r["evidence_id"]]
                                                   for r in rows}
                    used = {key for ids in child["allowed_candidates"].values() for key in ids}
                    child["dimensions"] = [d for d in payload["dimensions"] if d["dimension_id"] in used]
                elif stage == "revise":
                    child["evidence"], child["audit_examples"] = evidence_context.representatives(run, rows)
                    child["metrics"] = {d["dimension_id"]: payload["metrics"][d["dimension_id"]] for d in rows}
                await enqueue(repo, run, stage, child, split_key)
        elif stage == "match" and len(payload["evidence"]) == 1 and len(payload["dimensions"]) > 1:
            for child in match_pages.split_payload(payload, run["registry_revision"], run["stage"]):
                await enqueue(repo, run, stage, child, "evidence")
        else:
            run["issues"].append({"reason": "capacity_pending", "stage": stage,
                                  "case_ref": payload.get("case", {}).get("case_ref"),
                                  "payload_hash": sha(canonical(payload))})
        return
    run["planned_packets"][identity] = value["unit_id"]
    run["unit_count"] += 1
    identifier = value["unit_id"]
    run["queue"].append(identifier)
    for source in content.get("spans", []):
        version = source.get("case_version", content.get("source_versions", {}).get(source["case_ref"]))
        run.setdefault("source_spans", {})[source["span_ref"]] = {**source, "case_version": version}
    if omitted:
        run.setdefault("representative_selections", []).append({"unit_id": identifier, "stage": stage,
            "summary": content["representative_selection"], "omitted": omitted})
    metrics = run["metrics"]
    metrics["stage_units"][stage] = metrics["stage_units"].get(stage, 0) + 1
    metrics["peak_packet_chars"] = max(metrics["peak_packet_chars"], len(canonical(value)))
    await repo.save(run, **{identifier: {"packet": value, "attempt": 0, "status": "planned"}})


def groups(rows, count):
    return [rows[i:i + count] for i in range(0, len(rows), count)]


def unresolved(run):
    return [row for key, row in run["evidence"].items()
            if run["dispositions"].get(key) in {"unresolved", "unmatched", "deferred", "uncertain"}]


def spans(run, evidence, assessments=()):
    return evidence_context.ground(run, {"evidence": evidence, "audit_examples": list(assessments)})["spans"]


def representatives(run, dimensions):
    evidence, assessments = evidence_context.representatives(run, dimensions)
    return evidence, spans(run, evidence, assessments)


async def plan(engine, job, repo, run):
    stage, options = run["stage"], run["options"]
    dimensions = current(run)
    if stage == "extract":
        for case in run["cases"].values():
            if case_key(case) not in run["extracted"]:
                await enqueue(repo, run, "extract", {"case": model_view(case)})
    elif stage in {"bootstrap", "novelty"}:
        rows = unresolved(run)
        if stage == "bootstrap":
            selected, point_counts = set(), {}
            cases = sorted(run["cases"].values(), key=lambda c: sha(canonical([c["scene"], c["dialogue_text"]])))
            usable = {case_key(row) for row in rows}
            for case in cases:
                point = (case["course"], case["chapter"], case["knowledge_point"])
                if (case_key(case) in usable and point_counts.get(point, 0) < options["seed_cases_per_point"]
                        and len(selected) < options["seed_case_limit"]):
                    selected.add(case_key(case))
                    point_counts[point] = point_counts.get(point, 0) + 1
            rows = [row for row in rows if case_key(row) in selected]
        for batch in await neighbors(engine, job, repo, run, rows, options["propose_items"]):
            await enqueue(repo, run, "propose", {"purpose": stage, "evidence": batch, "dimensions": [],
                                                 "spans": spans(run, batch)}, "evidence")
    elif stage in {"match", "expanded_match", "rematch", "revision_match"}:
        rows = unresolved(run)
        if stage == "revision_match":
            rows = [row for key, row in run["evidence"].items() if run["dispositions"].get(key) != "routine"] \
                if run["changed"] else []
        limit = options["candidate_top_k"] if stage == "match" else options["expanded_candidate_limit"]
        candidates = await recall(engine, job, repo, run, rows, dimensions, limit)
        for batch in groups(rows, options["match_items"]):
            allowed = {row["evidence_id"]: candidates[row["evidence_id"]] for row in batch}
            used = {key for values in allowed.values() for key in values}
            await enqueue(repo, run, "match", {"evidence": batch, "dimensions": [d for d in dimensions
                          if d["dimension_id"] in used], "allowed_candidates": allowed}, "evidence")
    elif stage in {"audit", "recheck"}:
        if stage == "recheck":
            dimensions = [d for d in dimensions if d["dimension_id"] in run["changed"]]
        for case in run["cases"].values():
            for batch in groups(dimensions, options["audit_dimensions"]):
                await enqueue(repo, run, "audit", {"case": model_view(case), "dimensions": batch}, "dimensions")
    elif stage in {"gap_review", "gap_review_final"}:
        for gap in run["gaps"]:
            if gap.get("status") in {"covered", "new_candidate", "needs_review"}:
                continue
            for i, batch in enumerate(groups(dimensions, options["register_page_dimensions"])):
                await enqueue(repo, run, "register", {"proposal": audit_gaps.proposal(gap), "dimensions": batch,
                    "page_id": str(i), "gap_ref": gap["member_ref"]}, "dimensions")
    elif stage == "revise":
        for batch in await neighbors(engine, job, repo, run, dimensions, 3):
            evidence, assessments = evidence_context.representatives(run, batch)
            metrics = {d["dimension_id"]: counts(run, d) for d in batch}
            await enqueue(repo, run, "revise", {"dimensions": batch, "evidence": evidence,
                "audit_examples": assessments, "metrics": metrics, "audit_gaps": []}, "dimensions")
        for gap in run["gaps"]:
            if gap.get("status") == "new_candidate":
                await enqueue(repo, run, "revise", {"dimensions": [], "evidence": [], "audit_examples": [],
                    "metrics": {}, "audit_gaps": [gap]})
    elif stage == "rubric":
        for dimension in dimensions:
            evidence, assessments = evidence_context.representatives(run, [dimension])
            methods = sorted({m["method_text"] for m in run["memberships"]
                              if m["dimension_id"] == dimension["dimension_id"] and
                              m.get("active", True) and
                              m["version"] == dimension["version"] and m["method_text"]})
            await enqueue(repo, run, "rubric", {"dimension": dimension, "evidence": evidence,
                                                "audit_examples": assessments,
                                                "methods": methods, "scale": [0, 1, 2, 3]})
    run["planned"] = True
    await repo.save(run)


def counts(run, dimension):
    rows = {case_key(a): a for a in run["audits"] if a["dimension_id"] == dimension["dimension_id"]
            and a["version"] == dimension["version"]}
    counts_ = {signal: sum(row["signal"] == signal for row in rows.values()) for signal in
               ("support", "mixed", "counter", "not_demonstrated", "na", "ne")}
    return {**counts_, "covered": len(rows), "planned": len(run["cases"]),
            "supported_cases": counts_["support"] + counts_["mixed"],
            "denominator": sum(row["applicable"] == "yes" and row["sufficient"] == "yes" for row in rows.values())}


async def registration(engine, job, repo, run):
    active = run.get("register_proposal")
    if active:
        proposal = next(p for p in run["proposals"] if p["proposal_id"] == active)
        if run["queue"] or run.get("active"):
            await executor.step(engine, job, repo, run, consume)
            return
        register(run, proposal, run["register_comparisons"])
        run["register_proposal"] = None
        await repo.save(run)
        return
    proposal = next((p for p in run["proposals"] if p["status"] == "planned"), None)
    if proposal is None:
        finalize_splits(run)
        run.update(stage=run.pop("after_register"), planned=False)
        await repo.save(run)
        return
    run["register_proposal"] = proposal["proposal_id"]
    run["register_comparisons"] = []
    dimensions = [d for d in current(run) if d["dimension_id"] not in proposal.get("replace_sources", [])]
    for i, batch in enumerate(groups(dimensions, run["options"]["register_page_dimensions"])):
        await enqueue(repo, run, "register", {"proposal": proposal, "dimensions": batch, "page_id": str(i)},
                      "dimensions")
    await repo.save(run)


def advance(run):
    stage = run["stage"]
    if stage in {"audit", "recheck"}:
        audit_gaps.consolidate(run)
    elif stage in {"gap_review", "gap_review_final"}:
        audit_gaps.finalize(run)
    if stage == "revise":
        for identifier, status in run.pop("pending_demotions", {}).items():
            run["registry_revision"] += 1
            dimension = run["dimensions"][identifier]
            dimension.update(status=status, registry_revision=run["registry_revision"])
            run["versions"].append(dict(dimension))
            run["changed"].append(identifier)
    following = {"extract": "bootstrap", "match": "expanded_match", "audit": "gap_review", "gap_review": "revise",
                 "revision_match": "recheck", "recheck": "gap_review_final", "gap_review_final": "rubric",
                 "rubric": "assemble"}
    if stage in {"bootstrap", "novelty", "revise"}:
        run["after_register"] = {"bootstrap": "match", "novelty": "rematch", "revise": "revision_match"}[stage]
        run["stage"] = "register"
    elif stage in {"expanded_match", "rematch"}:
        created = run["metrics"]["register_created"]
        no_new = stage == "rematch" and created == run.get("previous_created")
        if unresolved(run) and run["novelty_round"] < run["options"]["novelty_rounds"] and not no_new:
            run.update(stage="novelty", novelty_round=run["novelty_round"] + 1, previous_created=created)
        else:
            run["stage"] = "audit"
    else:
        run["stage"] = following[stage]
    run["planned"] = False


async def tick(engine, job, action="poll"):
    repo = Repository(engine, job)
    run = await repo.get("run")
    if action == "stop":
        job = await update(engine, job, ov_cancel_reason="用户手动停止", status="stopping")
    if job.get("ov_cancel_reason") and (not run or not run.get("active")):
        return await update(engine, job, status="stopped", delivery_status="partial")
    if run is None:
        run = await prepare(engine, job)
        await repo.save(run)
    if run["skill_hash"] != sha(job["compiler_body_snapshot"]):
        raise ValueError("frozen_skill_changed")
    if job.get("ov_cancel_reason") or run.get("active"):
        return await executor.step(engine, job, repo, run, consume)
    await update(engine, job, dialogue_stage=run["stage"], processed_case_count=len(run["extracted"]),
                 total_case_count=len(run["cases"]), evidence_count=len(run["evidence"]),
                 assigned_evidence_count=sum(v == "attached" for v in run["dispositions"].values()),
                 pending_evidence_count=len(unresolved(run)), dimension_count=len(current(run)),
                 audit_completed_count=sum(counts(run, d)["covered"] for d in current(run)),
                 audit_planned_count=len(run["cases"]) * len(current(run)), rubric_count=len(run["rubrics"]),
                 issue_count=len(run["issues"]), registry_revision=run["registry_revision"])
    if run["stage"] == "register":
        return await registration(engine, job, repo, run)
    if run["stage"] in {"assemble", "finished"}:
        from .delivery import finish
        return await finish(engine, job, repo, run)
    if run["queue"]:
        return await executor.step(engine, job, repo, run, consume)
    if not run["planned"]:
        await plan(engine, job, repo, run)
    else:
        advance(run)
        await repo.save(run)
