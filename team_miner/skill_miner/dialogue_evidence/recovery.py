"""Bounded field repair with a durable usable receipt for partial fallback."""

from ..dialogue_dimensions.input import sha


async def accept(repo, run, unit, candidate, consume, job_id):
    """Consume once, then atomically commit Evidence and its source receipt."""
    if unit.get("error"):
        unit["last_attempt_error"] = unit["error"]
    consume(run, unit, candidate["receipt"], candidate["receipt_hash"], job_id)
    unit.update(status="accepted", error="", receipt=candidate["receipt"],
                receipt_hash=candidate["receipt_hash"], remote_task_id=candidate["task_id"],
                receipt_uri=candidate["uri"])
    unit.pop("fallback", None)
    run["metrics"]["accepted"] += 1
    await repo.save(run, **{unit["packet"]["unit_id"]: unit})


async def handle_receipt(repo, run, unit, receipt, text, active, uri, consume, job_id):
    unit["processed_task_id"] = active["task_id"]
    candidate = {"receipt": receipt, "receipt_hash": sha(text), "task_id": active["task_id"], "uri": uri}
    fallback = unit.get("fallback")
    if receipt["quarantined"]:
        if fallback is None or len(receipt["quarantined"]) < len(fallback["receipt"]["quarantined"]):
            unit["fallback"] = candidate
        can_repair = (unit.get("field_repairs", 0) < run["options"].get("field_repair_attempts_per_unit", 0)
                      and unit["attempt"] < run["options"]["repair_attempts_per_unit"])
        if can_repair:
            unit["field_repairs"] = unit.get("field_repairs", 0) + 1
            unit["attempt"] += 1
            errors = [{k: issue.get(k) for k in ("local_ref", "field", "reason", "detail")}
                      for issue in receipt["quarantined"]]
            from ..dialogue_dimensions.input import canonical
            unit.update(status="retry_wait", failure_kind="field", error=canonical(errors)[:2000])
            run["metrics"]["retries"] += 1
            run["metrics"]["field_repairs"] = run["metrics"].get("field_repairs", 0) + 1
            await repo.save(run, **{unit["packet"]["unit_id"]: unit})
            return
        candidate = unit["fallback"]
    await accept(repo, run, unit, candidate, consume, job_id)
