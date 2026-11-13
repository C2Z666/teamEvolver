"""Deterministic self-contained JSONL delivery with source and checksum validation."""

from ..dialogue_dimensions.input import canonical, case_key, normalize_case, sha
from ..dialogue_dimensions.orchestration import update
from ..dialogue_dimensions.transport import artifact
from ..mining_contracts import utc_now
from .schemas import Coverage, ExportEvidence, Manifest, Receipt, SourceCase, Task


def coverage(run, units):
    rows = []
    for case in run["cases"].values():
        leaves = [u for u in units if case_key(u["packet"]) == case_key(case) and u["status"] != "superseded"]
        accepted = [u for u in leaves if u["status"] == "accepted"]
        owned = [s for u in leaves for s in u["packet"]["payload"]["scope"]["owned_sentence_ids"]]
        expected = [s["sentence_id"] for s in case["sentence_index"]]
        if sorted(owned) != sorted(expected) or len(set(owned)) != len(owned):
            raise ValueError("leaf_ownership_not_conserved")
        count = sum(case_key(e) == case_key(case) for e in run["evidence"].values())
        quarantined = sum(len(u.get("receipt", {}).get("quarantined", [])) for u in accepted)
        blocked = any(i["reason"] in {"source_changed", "missing_source", "input_incomplete"}
                      for u in accepted for i in u["receipt"]["issues"])
        outcome = ("complete" if count else "zero_evidence") if len(accepted) == len(leaves) and not (
            quarantined or blocked) else "partial" if accepted else "failed"
        rows.append({"case_ref": case["case_ref"], "case_version": case["case_version"], "outcome": outcome,
            "evidence_count": count, "quarantined_count": quarantined,
            "units": [{"unit_id": u["packet"]["unit_id"], "status": u["status"],
                       "owned_sentence_ids": u["packet"]["payload"]["scope"]["owned_sentence_ids"],
                       "reason": u.get("error", "")} for u in leaves]})
    return rows


def build(job, run, units):
    covered = coverage(run, units)
    status = "complete" if all(c["outcome"] in {"complete", "zero_evidence"} for c in covered) else (
        "partial" if any(u["status"] == "accepted" for u in units) else "failed")
    if job.get("ov_cancel_reason"):
        status = "cancelled"
        for row in covered:
            if row["outcome"] in {"partial", "failed"}:
                row["outcome"] = "cancelled"
    if any(i.get("status") == "input_pending" for i in run["issues"]):
        status = "partial" if status == "complete" else status
    for row in covered:
        Coverage.model_validate(row)
    for case in run["cases"].values():
        SourceCase.model_validate(case)
        if normalize_case(case) != case:
            raise ValueError("export_source_changed")
    for evidence in run["evidence"].values():
        ExportEvidence.model_validate(evidence)
        if case_key(evidence) not in run["cases"]:
            raise ValueError("export_foreign_case")
    files = {}
    collections = {"evidence": list(run["evidence"].values()), "cases": list(run["cases"].values()),
                   "coverage": covered, "issues": run["issues"] + [
                       {"unit_id": u["packet"]["unit_id"], "reason": u.get("error", "failed")}
                       for u in units if u["status"] in {"failed", "deferred"}]}
    for name, rows in collections.items():
        parts, text = [], ""
        for row in rows:
            line = canonical(row)+"\n"
            if text and len((text+line).encode()) > 512*1024:
                parts.append(text)
                text = ""
            text += line
        parts.append(text)
        for i, part in enumerate(parts, 1):
            path = f"{name}-{i:04d}.jsonl" if name in {"evidence", "cases"} else f"{name}.jsonl"
            if name in {"coverage", "issues"} and len(parts) > 1:
                path = f"{name}-{i:04d}.jsonl"
            files[path] = part
    for name, model in (("task", Task), ("receipt", Receipt), ("evidence", ExportEvidence),
                        ("case", SourceCase), ("coverage", Coverage), ("manifest", Manifest)):
        files[f"schemas/{name}.json"] = canonical(model.model_json_schema())+"\n"
    files["README.md"] = (
        "# 行为 Evidence 导出\n\n先校验 manifest 文件摘要、字节数和 JSONL 行数，再按 case_ref + case_version "
        "关联 cases 与 Evidence。句子索引使用 Unicode code points，零起点、左闭右开；按 sentence_id "
        "截取原文，不合并不连续引文。observed_conditions 为来源事实，suggested_applicability 为推断。"
        "不生成评分或维度。coverage 包含全部案例，partial/failed 不可当完整来源。"
        "Evidence ID 标识本次提取，跨任务用 dataset_hash 和案例身份检查血缘。\n")
    files["run_metrics.json"] = canonical({**run["metrics"], "budget_profile": run["budget_profile"],
                                           "adaptive": run.get("adaptive")})+"\n"
    manifest = {"schema_version": "taikang.evidence-delivery.v1", "delivery_status": status,
        "job_id": job["job_id"], "dataset_hash": run["dataset_hash"], "skill_hash": run["skill_hash"],
        "execution_hash": run["execution_hash"], "renderer_version": 1,
        "config_revision": job.get("config_snapshot", {}).get("settings_revision"),
        "options": run["options"], "case_count": len(covered), "evidence_count": len(run["evidence"]),
        "raw_evidence_count": sum(u.get("receipt", {}).get("raw_evidence_count", 0) for u in units),
        "quarantined_evidence_count": sum(i["reason"] == "isolated_evidence" for i in run["issues"]),
        "files": [{"path": p, "sha256": sha(t), "bytes": len(t.encode()),
                   **({"rows": len(t.splitlines())} if p.endswith(".jsonl") else {})} for p, t in files.items()]}
    if manifest["raw_evidence_count"] != manifest["evidence_count"] + manifest["quarantined_evidence_count"]:
        raise ValueError("evidence_count_not_conserved")
    Manifest.model_validate(manifest)
    files["manifest.json"] = canonical(manifest)+"\n"
    return files, manifest


async def finish(engine, job, repo, run, units):
    files, manifest = build(job, run, units)
    for path, text in files.items():
        await artifact(engine, job, path, text)
    run["stage"] = "finished"
    await repo.save(run)
    status = manifest["delivery_status"]
    return await update(engine, job, delivery_status=status,
        status={"complete": "succeeded", "partial": "interrupted", "failed": "failed", "cancelled": "stopped"}[status],
        dialogue_stage="finished", materialized=True, ov_task_id="", compile_request=None,
        finished_at=utc_now(), phase={"step1": "done", "step2": "done", "step3": "done"})
