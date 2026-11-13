"""Fenced single-stage preparation, extraction, bounded recovery and delivery."""

from datetime import datetime, timezone

from ..dialogue_dimensions.input import canonical, case_key, sha
from ..dialogue_dimensions.orchestration import update
from ..mining_contracts import utc_now
from ..ov_round_orchestration import _compiler_package
from . import PROTOCOL, executor
from .budget import DEFAULTS, divide, plan
from .compiler import sections, stage_body
from .preparation import load_cases
from .repository import Repository


async def prepare(engine, job, repo):
    sections(job["compiler_body_snapshot"])
    cases, issues, dataset_hash = await load_cases(engine, job)
    options = dict(DEFAULTS)
    options.update(job.get("evidence_options", {}))
    run = {"execution_protocol": PROTOCOL, "stage": "extract", "cases": cases, "issues": issues,
           "dataset_hash": dataset_hash, "skill_hash": sha(job["compiler_body_snapshot"]),
           "execution_hash": sha(_compiler_package(stage_body(job["compiler_body_snapshot"], "extract"))[1]),
           "options": options, "queue": [], "units": [], "active": None, "evidence": {},
           "started_at": utc_now(), "unit_count": 0,
           "budget_profile": {"estimator": "unicode-chars-and-utf8-v1", "model": "unknown",
               "context_tokens": "unknown", "max_tokens": "unknown", "account_tpm": "unknown",
               "skill_chars": len(stage_body(job["compiler_body_snapshot"], "extract")),
               "calibration": "conservative_uncalibrated_no_batch_expansion"},
           "metrics": {"compiles": 0, "accepted": 0, "retries": 0, "splits": 0}}
    records = {}
    for case in cases.values():
        units = plan(case, options, run["unit_count"]+1)
        for i, unit in enumerate(units, 1):
            value = unit["packet"]
            value["payload"]["scope"].update(segment_index=i, segment_count=len(units))
            value.pop("input_hash")
            value["input_hash"] = sha(canonical(value))
            records[value["unit_id"]] = unit
            run["units"].append(value["unit_id"])
            run["queue"].append(value["unit_id"])
        run["unit_count"] += len(units)
    run["max_calls"] = max(1, run["unit_count"]) * (2 ** (options["capacity_split_depth"]+1)-1) * (
        options["repair_attempts_per_unit"]+options["transport_retry_attempts"]+1)
    run["deadline_seconds"] = job["runtime_timeout_seconds"] * max(1, len(cases))
    await repo.save(run, **records)
    return run


async def split_unit(repo, run, unit):
    task = unit["packet"]
    case = run["cases"][case_key(task)]
    parts = divide(case, task["payload"]["scope"]["owned_sentence_ids"])
    if not parts or unit["depth"] >= run["options"]["capacity_split_depth"]:
        unit.update(status="failed", error="capacity_exceeded")
        await repo.save(run, **{task["unit_id"]: unit})
        return
    records = {}
    for part in parts:
        for child in plan(case, run["options"], run["unit_count"]+1, part, unit["depth"]+1):
            run["unit_count"] += 1
            identifier = child["packet"]["unit_id"]
            child["parent"] = task["unit_id"]
            records[identifier] = child
            run["units"].append(identifier)
            run["queue"].append(identifier)
    for index, child in enumerate(records.values(), 1):
        value = child["packet"]
        value["payload"]["scope"].update(segment_index=index, segment_count=len(records))
        value.pop("input_hash")
        value["input_hash"] = sha(canonical(value))
    unit["status"] = "superseded"
    records[task["unit_id"]] = unit
    run["metrics"]["splits"] += 1
    await repo.save(run, **records)


def consume(run, unit, receipt, receipt_hash, job_id):
    task = unit["packet"]
    for row in receipt["result"]["evidence"]:
        identifier = "ev-" + sha(canonical([job_id, task["unit_id"], receipt_hash, row["local_ref"]]))
        run["evidence"][identifier] = {**row, "schema_version": "taikang.evidence.v1", "evidence_id": identifier,
            "job_id": job_id, "unit_id": task["unit_id"], "case_ref": task["case_ref"],
            "case_version": task["case_version"], "receipt_hash": receipt_hash,
            "skill_hash": run["skill_hash"], "dataset_hash": run["dataset_hash"],
            "source_scope": task["payload"]["scope"]}
    for issue in receipt["issues"] + receipt["quarantined"]:
        run["issues"].append({**issue, "unit_id": task["unit_id"], "case_ref": task["case_ref"],
                              "case_version": task["case_version"]})


async def tick(engine, job, action="poll"):
    repo = Repository(engine, job)
    run = await repo.get("run")
    if action == "stop":
        job = await update(engine, job, ov_cancel_reason="用户手动停止", status="stopping")
    if run is None:
        run = await prepare(engine, job, repo)
    if run["skill_hash"] != sha(job["compiler_body_snapshot"]):
        raise ValueError("frozen_skill_changed")
    if run.get("active"):
        return await executor.step(engine, job, repo, run, consume)
    elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(run["started_at"])).total_seconds()
    exhausted = elapsed > run["deadline_seconds"] or run["metrics"]["compiles"] >= run["max_calls"]
    units = [await repo.get(identifier) for identifier in run["units"]]
    if job.get("ov_cancel_reason") or exhausted:
        for unit in units:
            if unit["status"] not in {"accepted", "failed", "superseded"}:
                unit.update(status="failed", error="cancelled" if job.get("ov_cancel_reason") else "deadline_exceeded")
                await repo.save(run, **{unit["packet"]["unit_id"]: unit})
        run["queue"] = []
    from .delivery import coverage, finish
    covered = coverage(run, units)
    await update(engine, job, dialogue_stage="extract", total_case_count=len(run["cases"]),
        processed_case_count=sum(c["outcome"] in {"complete", "zero_evidence"} for c in covered),
        evidence_count=len(run["evidence"]), issue_count=len(run["issues"]),
        zero_evidence_case_count=sum(c["outcome"] == "zero_evidence" for c in covered),
        quarantined_evidence_count=sum(i["reason"] == "isolated_evidence" for i in run["issues"]),
        failed_unit_count=sum(u["status"] in {"failed", "deferred"} for u in units),
        evidence_unit_count=sum(u["status"] != "superseded" for u in units),
        evidence_accepted_units=sum(u["status"] == "accepted" for u in units),
        evidence_compile_count=run["metrics"]["compiles"])
    pending = [u for u in units if u["status"] not in {"accepted", "failed", "deferred", "superseded"}]
    if not pending:
        return await finish(engine, job, repo, run, units)
    await executor.step(engine, job, repo, run, consume)
