"""Bounded file Map submission, reconciliation and partial child recovery."""

from datetime import datetime, timedelta, timezone

from starlette.exceptions import HTTPException

from ..compile_admission import TERMINAL_NATIVE_STATES
from ..dialogue_dimensions.input import canonical, sha
from ..dialogue_dimensions.orchestration import update
from ..dialogue_dimensions.transport import descendant, read, write
from ..mining_contracts import RUNS_ROOT, utc_now
from ..ov_round_orchestration import _compiler_package
from . import adaptive, recovery
from .compiler import stage_body
from .validation import validate

TRANSPORT_INSTRUCTION = (
    "During Entry immediately emit needs_pipeline=true, files=[]. Never solve in Entry. "
    "Planner must use exactly one file Map then Finalize, execution=agent, input_unit=file, "
    "output=files, fields={}. Each child reads exactly one task file and emits task.output_path "
    "with complete taikang.evidence-result.v1 JSON in FileResponse content. "
    "FileResponse inputs use assignment input IDs, not business IDs. "
    "Copy the entire Skill business rules and required receipt schema into each Transform. "
    "Copy unit_id, stage, input_hash, case_ref, case_version exactly from task. "
    "No Reduce, Shuffle, second Map, cross-case synthesis or scoring. "
    "No evidence quota or truncated output; report needs_split when capacity is insufficient."
)


def elapsed(value):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()


async def step(engine, job, repo, run, consume):
    """One network transition per tick; accepted children and domain changes commit together."""
    cancelling = bool(job.get("ov_cancel_reason"))
    active = run.get("active")
    if active is None:
        if cancelling:
            return
        not_before = run.get("compile_not_before")
        if not_before and elapsed(not_before) < 0:
            return
        units, total = [], 0
        for identifier in run["queue"]:
            unit = await repo.get(identifier)
            if unit["status"] in {"accepted", "failed", "deferred", "superseded"}:
                continue
            size = len(canonical(unit["packet"]))
            if units and (len(units) >= adaptive.batch_limit(run, job) or
                          total + size > run["options"]["compile_input_chars"] or
                          unit["packet"]["stage"] != units[0]["packet"]["stage"]):
                break
            units.append(unit)
            total += size
        if not units:
            run["queue"] = []
            await repo.save(run)
            return
        sliced = stage_body(job["compiler_body_snapshot"], units[0]["packet"]["stage"])
        name, published, _ = _compiler_package(sliced)
        await engine._round_provision_compiler(engine._round_client(job), name, published)
        key = "te-s3-" + sha(canonical([engine.tenant, job["job_id"], run["skill_hash"],
                                     [[u["packet"]["input_hash"], u["attempt"],
                                       *([u["transport_attempt"]] if u.get("transport_attempt") else [])]
                                      for u in units]]))
        root = f"{RUNS_ROOT}/{job['job_id']}/s3/{key}"
        for unit in units:
            unit["stage_hash"] = sha(published)
            unit["source_skill_hash"] = run["skill_hash"]
            unit["request_key"] = key
            unit["status"] = "submitting"
            await write(engine, job, root + "-input/" + unit["packet"]["unit_id"] + ".md",
                        canonical(unit["packet"]))
        entries = await engine._knowledge_request(job["account"], "GET", "/api/v1/fs/ls", timeout=30,
            params={"uri": root + "-input", "recursive": "false", "output": "original",
                    "show_all_hidden": "false", "node_limit": len(units) + 1})
        expected = {root + "-input/" + unit["packet"]["unit_id"] + ".md" for unit in units}
        if not isinstance(entries, list) or len(entries) != len(expected) or any(
                row.get("isDir") or row.get("access") == "denied" for row in entries) or {
                row.get("uri") for row in entries} != expected:
            raise ValueError("s3_input_directory_changed")
        instruction = TRANSPORT_INSTRUCTION
        repairs = [{"unit_id": u["packet"]["unit_id"], "validation_error": u["error"]}
                   for u in units if u.get("error") and u.get("failure_kind") != "transport"]
        if repairs:
            instruction += "\nRepair failed assignments, preserving frozen identity: " + canonical(repairs)
        active = {"key": key, "units": [u["packet"]["unit_id"] for u in units], "submitted_at": utc_now(),
                  "request": {"from": [root + "-input"], "to": root, "skill": "viking://agent/skills/" + name,
                              "instruction": instruction}}
        run["active"] = active
        await repo.save(run, **{u["packet"]["unit_id"]: u for u in units})
    if not active.get("task_id"):
        if not await engine.admission.acquire(job, {"key": active["key"]}):
            return
        await update(engine, job, status="preparing", submission_idempotency_key=active["key"],
                     compile_request=active["request"], round_started_at=active["submitted_at"])
        try:
            accepted = await engine._knowledge_request(job["account"], "GET",
                "/api/v1/compile/submissions/" + active["key"], timeout=10)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # Even at the retry limit, reconcile a possibly accepted submission first.
            if active.get("transport_attempt", 0) > run["options"]["transport_retry_attempts"]:
                for identifier in active["units"]:
                    unit = await repo.get(identifier)
                    if unit.get("fallback"):
                        await recovery.accept(repo, run, unit, unit["fallback"], consume, job["job_id"])
                    else:
                        unit.update(status="failed", error="submission_status_unknown")
                        run["issues"].append({"unit_id": identifier, "reason": "submission_status_unknown"})
                        await repo.save(run, **{identifier: unit})
                await engine.admission.release(key=active["key"])
                run["active"] = None
                await repo.save(run)
                return
            try:
                accepted = await engine._knowledge_request(job["account"], "POST", "/api/v1/compile", timeout=60,
                    extra_headers={"Idempotency-Key": active["key"]}, json=active["request"])
            except (TimeoutError, ConnectionError):
                active["transport_attempt"] = active.get("transport_attempt", 0) + 1
                await repo.save(run)
                raise
            except HTTPException as rejection:
                if rejection.status_code not in {400, 401, 403, 413, 422}:
                    # Includes 429: same key, no semantic retry consumption.
                    active["status"] = "reconciling"
                    active["transport_attempt"] = active.get("transport_attempt", 0) + 1
                    await repo.save(run)
                    raise
                await engine.admission.release(key=active["key"])
                for identifier in active["units"]:
                    unit = await repo.get(identifier)
                    if unit.get("fallback"):
                        unit["error"] = "compile_request_rejected"
                        await recovery.accept(repo, run, unit, unit["fallback"], consume, job["job_id"])
                        continue
                    unit.update(status="failed", error="compile_request_rejected")
                    run["issues"].append({"unit_id": identifier, "reason": unit["error"],
                                          "http_status": rejection.status_code})
                    await repo.save(run, **{identifier: unit})
                run["active"] = None
                await repo.save(run)
                return
        task_id = accepted.get("task_id")
        if not task_id:
            raise ValueError("compile_acceptance_missing_task_id")
        active.update(task_id=task_id, status="running")
        run["metrics"]["compiles"] += 1
        await repo.save(run)
        await engine.admission.mark({"key": active["key"], "task_id": task_id})
        await update(engine, job, status="running", ov_task_id=task_id, started_at=job.get("started_at") or utc_now())
        return
    task = await engine._knowledge_request(job["account"], "GET", "/api/v1/tasks/" + active["task_id"], timeout=10)
    if task.get("task_id", active["task_id"]) != active["task_id"]:
        raise ValueError("foreign_remote_task")
    status = str(task.get("status", "")).lower()
    if status not in TERMINAL_NATIVE_STATES:
        deadline = run.get("deadline_seconds")
        exhausted = deadline is not None and elapsed(run["started_at"]) > deadline
        if ((cancelling or exhausted or elapsed(active["submitted_at"]) > adaptive.timeout_seconds(run, job))
                and not active.get("cancel")):
            await engine._knowledge_request(job["account"], "POST",
                "/api/v1/tasks/" + active["task_id"] + "/cancel", timeout=10)
            active["cancel"] = utc_now()
            await repo.save(run)
        if active.get("cancel") and elapsed(active["cancel"]) > 120:
            return await update(engine, job, status="interrupted", delivery_status="partial",
                                remote_terminal_unconfirmed=True, error="remote_cancel_unconfirmed",
                                finished_at=utc_now())
        await update(engine, job, status="stopping" if cancelling else "running", ov_task_id=active["task_id"],
                     remote_terminal_unconfirmed=bool(active.get("cancel")), ov_status=status)
        return
    await engine.admission.release(task_id=active["task_id"])
    if cancelling:
        run["active"] = None
        await repo.save(run)
        return await update(engine, job, status="stopping", remote_terminal_unconfirmed=False)
    failure_text = str(task.get("error") or task.get("message") or "").lower()
    rate_limited = status == "failed" and any(marker in failure_text for marker in (
        "modelaccounttpmratelimitexceeded", "toomanyrequests", "error code: 429", "rate_limit_exceeded"))
    capacity_failure = any(marker in failure_text for marker in (
        "context_length", "context window", "maximum context", "token budget", "input too long"))
    for identifier in active["units"]:
        unit = await repo.get(identifier)
        if unit["status"] == "accepted":
            text = await read(engine, job, unit.get("receipt_uri") or descendant(
                active["request"]["to"], unit["packet"]["output_path"]))
            if sha(text) != unit["receipt_hash"]:
                conflict = {"unit_id": identifier, "reason": "receipt_conflict", "receipt_hash": sha(text),
                            "accepted_receipt_hash": unit["receipt_hash"]}
                if conflict not in run["issues"]:
                    run["issues"].append(conflict)
                    await repo.save(run)
            continue
        if unit["status"] == "superseded":
            continue
        if unit.get("processed_task_id") == active["task_id"]:
            # A later child read may fail after this unit's retry decision committed.
            # Replaying the same Compile must not consume another repair attempt.
            continue
        original = await read(engine, job, active["request"]["from"][0] + "/" + identifier + ".md")
        if original != canonical(unit["packet"]):
            raise ValueError("frozen_s3_packet_changed")
        try:
            text = await read(engine, job, descendant(active["request"]["to"], unit["packet"]["output_path"]))
            receipt = validate(text, unit["packet"])
        except (ValueError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code != 404:
                raise
            unit.update(error=str(exc)[:1000], status="retry_wait", failure_kind="receipt",
                        processed_task_id=active["task_id"])
            if rate_limited and isinstance(exc, HTTPException) and exc.status_code == 404:
                unit["failure_kind"] = "transport"
                attempts = unit.get("transport_attempt", 0)
                limit = run["options"].get("transport_retry_attempts", 5)
                if attempts >= limit:
                    unit["status"] = "deferred"
                    run["issues"].append({"unit_id": identifier, "reason": "upstream_rate_limit_exhausted"})
                else:
                    unit["transport_attempt"] = attempts + 1
                    delay = run["options"].get("transport_retry_delay_seconds", 60) * min(2 ** attempts, 8)
                    deadline = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    if run.get("compile_not_before"):
                        deadline = max(deadline, datetime.fromisoformat(run["compile_not_before"]))
                    run["compile_not_before"] = deadline.isoformat()
                    run["metrics"]["transport_retries"] = run["metrics"].get("transport_retries", 0) + 1
                    run["issues"].append({"unit_id": identifier, "reason": "upstream_rate_limit_retry",
                                          "attempt": attempts + 1, "delay_seconds": delay})
                unit["error"] = failure_text[:1000]
            elif capacity_failure or str(exc) == "capacity_exceeded":
                if unit.get("fallback"):
                    await recovery.accept(repo, run, unit, unit["fallback"], consume, job["job_id"])
                    continue
                from .orchestration import split_unit
                await split_unit(repo, run, unit)
                continue
            elif unit["attempt"] >= run["options"]["repair_attempts_per_unit"]:
                if unit.get("fallback"):
                    await recovery.accept(repo, run, unit, unit["fallback"], consume, job["job_id"])
                    continue
                unit["status"] = "failed"
                run["issues"].append({"unit_id": identifier, "reason": "receipt_failed", "detail": unit["error"]})
            else:
                unit["attempt"] += 1
                run["metrics"]["retries"] += 1
            if unit["status"] == "deferred" and unit.get("fallback"):
                await recovery.accept(repo, run, unit, unit["fallback"], consume, job["job_id"])
                continue
            await repo.save(run, **{identifier: unit})
            continue
        await recovery.handle_receipt(repo, run, unit, receipt, text, active,
            descendant(active["request"]["to"], unit["packet"]["output_path"]), consume, job["job_id"])
    current_units = [await repo.get(identifier) for identifier in active["units"]]
    unhealthy = any(u["status"] not in {"accepted"} or u.get("receipt", {}).get("quarantined")
                    for u in current_units)
    adaptive.observe(run, job, task=task, active=active, seconds=elapsed(active["submitted_at"]), unhealthy=unhealthy)
    run["metrics"].setdefault("compile_wall_seconds", []).append({
        "request_key": active["key"], "task_id": active["task_id"],
        "seconds": elapsed(active["submitted_at"]), "status": status})
    tokens = (task.get("meta") or {}).get("pipeline", {}).get("tokens")
    if isinstance(tokens, dict):
        run["metrics"].setdefault("compile_token_usage", []).append({
            "task_id": active["task_id"], "tokens": tokens,
            "scope": "openviking_pipeline_reported_totals"})
        run["metrics"]["token_usage"] = "per_compile_when_reported"
    run["active"] = None
    await repo.save(run)
    await update(engine, job, status="preparing", ov_task_id="", compile_request=None,
                 remote_terminal_unconfirmed=False)
