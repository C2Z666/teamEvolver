"""Paged source-traceable candidate standards; never a published Skill Candidate."""

from ...mining_contracts import utc_now
from ..input import canonical, case_key, sha
from ..orchestration import update
from ..transport import artifact
from .input_view import model_view
from .orchestration import counts
from .registry import current


async def finish(engine, job, repo, run):
    dimensions = current(run)
    run["metrics"].update(
        register_updated=run["metrics"].get("register_updated", 0),
        input_cases=len(run["cases"]),
        raw_dialogue_chars=sum(len(c["dialogue_text"]) for c in run["cases"].values()),
        model_view_chars=sum(len(canonical(model_view(c))) for c in run["cases"].values()),
        evidence_count=len(run["evidence"]),
        disposition_counts={status: sum(value == status for value in run["dispositions"].values())
                            for status in sorted(set(run["dispositions"].values()))},
        audit_planned=len(run["cases"]) * len(dimensions),
        audit_completed=sum(counts(run, d)["covered"] for d in dimensions),
        final_rubric_receipts=len(run["rubrics"]),
        phase_wall_seconds="unavailable",
    )
    coverage = [{"kind": "extract", "case_ref": c["case_ref"], "case_version": c["case_version"],
                 "status": "accepted" if case_key(c) in run["extracted"] else "pending"}
                for c in run["cases"].values()]
    coverage += [{"kind": "evidence", "evidence_id": key, "status": run["dispositions"].get(key, "deferred")}
                 for key in run["evidence"]]
    coverage += [{"kind": "audit", "dimension_id": d["dimension_id"], "version": d["version"], **counts(run, d)}
                 for d in dimensions]
    coverage += [{"kind": "rubric", "dimension_id": d["dimension_id"], "version": d["version"],
                  "status": run["rubrics"].get(d["dimension_id"], {}).get("rubric_status", "pending")}
                 for d in dimensions]
    pending_evidence = sum(run["dispositions"].get(key) not in {"attached", "routine"} for key in run["evidence"])
    pending_gaps = sum(g.get("status") not in {"covered", "addressed"} for g in run["gaps"])
    pending_rubrics = sum(run["rubrics"].get(d["dimension_id"], {}).get("rubric_status") != "provisional"
                          or run["rubrics"].get(d["dimension_id"], {}).get("version") != d["version"]
                          for d in dimensions)
    pending_revisions = sum(bool(p.get("revision")) and p["status"] != "registered" for p in run["proposals"])
    partial = (len(run["extracted"]) != len(run["cases"]) or
               any(counts(run, d)["covered"] != len(run["cases"]) for d in dimensions) or
               pending_evidence > 0 or pending_gaps > 0 or pending_rubrics > 0 or pending_revisions > 0 or
               any(i.get("reason") in {"model_unit_budget_exhausted", "capacity_pending", "receipt_failed",
                                       "compile_request_rejected", "revision_conflict", "revision_identity_conflict",
                                       "incomplete_split_parent_retained", "upstream_rate_limit_exhausted"}
                   or i.get("status") == "input_pending"
                   for i in run["issues"]))
    validation = {"delivery_status": "partial" if partial else "complete", "validation_status": "passed_with_warnings",
                  "content_validated": False, "independent_calibration": "pending", "review_status": "recommended",
                  "processed_case_count": len(run["extracted"]),
                  "pending_case_count": len(run["cases"])-len(run["extracted"]),
                  "pending_evidence_count": pending_evidence, "pending_gap_count": pending_gaps,
                  "pending_rubric_count": pending_rubrics,
                  "pending_revision_count": pending_revisions,
                  "rubric_readiness": "pending" if pending_rubrics else "provisional",
                  "dimension_count": len(dimensions)}
    entries = []

    async def emit(path, text):
        await artifact(engine, job, path, text)
        entries.append({"path": path, "sha256": sha(text), "bytes": len(text.encode())})

    collections = {"dimensions": list(run["dimensions"].values()), "rubrics": list(run["rubrics"].values()),
                   "findings": list(run["evidence"].values()), "memberships": run["memberships"],
                   "audit": run["audits"], "coverage": coverage, "issues": run["issues"] + run["gaps"],
                   "proposals": run["proposals"],
                   "lineage": run["lineage"] + run["versions"],
                   "methods": [m for m in run["memberships"] if m["relation"] == "method_of" or m["method_text"]]}
    # Frozen raw sources and sentence coordinates make every delivered span independently recoverable.
    collections["sources"] = list(run["cases"].values())
    collections["spans"] = list(run.get("source_spans", {}).values())
    collections["representative_selections"] = run.get("representative_selections", [])
    collections["gap_reviews"] = [{"member_ref": key, "comparisons": rows}
                                  for key, rows in run.get("gap_comparisons", {}).items()]
    collections["match_reviews"] = list(run.get("match_scopes", {}).values())
    for name, rows in collections.items():
        for offset in range(0, len(rows), 50):
            batch = rows[offset:offset + 50]
            jsonl = name not in {"dimensions", "rubrics"}
            text = "".join(canonical(row) + "\n" for row in batch) if jsonl else canonical(batch) + "\n"
            await emit(f"{name}/part-{offset // 50 + 1:06d}." + ("jsonl" if jsonl else "json"), text)
    await emit("validation.json", canonical(validation) + "\n")
    await emit("run_metrics.json", canonical(run["metrics"]) + "\n")
    await emit("index.md", "# 优秀对话候选评分标准\n\n"
               f"交付状态：{validation['delivery_status']}。已分析 {len(run['extracted'])}/{len(run['cases'])} 条；"
               f"评分维度 {len(dimensions)} 个。\n\n"
               "评分标准为 provisional/pending，尚未独立校准。N/A 与 NE 不作零分；方法实例不单独重复计分。"
               "来源见 sources，Evidence 见 findings，适用性与反证见 audit，完整性见 coverage。"
               "未计算案例分数、权重或总分，也不创建 Skill Candidate。\n")
    manifest = {"schema_version": "taikang.dimension-delivery.v2", **validation, "files": entries,
                "dataset_hash": run["dataset_hash"], "skill_hash": run["skill_hash"],
                "config_revision": job.get("config_snapshot", {}).get("settings_revision"),
                "registry_revision": run["registry_revision"], "embedding_model": run["embedding_model"],
                "options": run["options"], "model_units": run["unit_count"]}
    await artifact(engine, job, "manifest.json", canonical(manifest) + "\n")
    run["stage"] = "finished"
    await repo.save(run)
    return await update(engine, job, **validation, status="interrupted" if partial else "succeeded",
                        dialogue_stage="finished", materialized=True, ov_task_id="", compile_request=None,
                        finished_at=utc_now(), phase={"step1": "done", "step2": "done", "step3": "done"})
