"""Persisted, bounded batch tuning using completed Compile consumption only."""

import math


def initial_timeout_seconds(run, job):
    if run["options"].get("runtime_timeout_is_job_deadline"):
        return run["options"]["compile_initial_timeout_seconds"]
    return job["runtime_timeout_seconds"]


def policy(run, job):
    options = run["options"]
    if not options.get("adaptive_batching", 0):
        return None
    return run.setdefault("adaptive", {
        "batch_limit": options["packets_per_compile"], "clean_streak": 0, "recent": [],
        "timeout_seconds": initial_timeout_seconds(run, job), "decisions": [],
        "initial_batch_limit": options["packets_per_compile"], "max_batch_limit": options["adaptive_batch_max"],
        "policy_version": options.get("adaptive_policy_version", 1),
    })


def batch_limit(run, job):
    current = policy(run, job)
    return current["batch_limit"] if current else run["options"]["packets_per_compile"]


def timeout_seconds(run, job):
    current = policy(run, job)
    return current["timeout_seconds"] if current else initial_timeout_seconds(run, job)


def token_total(task):
    tokens = (task.get("meta") or {}).get("pipeline", {}).get("tokens")
    if not isinstance(tokens, dict):
        return None
    values = [tokens.get(k) for k in ("input", "output")]
    if any(type(v) is not int or v < 0 for v in values):
        return None
    return sum(values) or None


def observe(run, job, *, task, active, seconds, unhealthy=False, resource_pressure=None):
    """Grow by one after clean runs; back off immediately on pressure or failures.

    Missing usage inhibits growth. Cached tokens are a subset of input, never added.
    A decision is saved in the same fenced CAS transition as clearing the Compile.
    """
    current = policy(run, job)
    if current is None:
        return
    if any(d["task_id"] == active["task_id"] for d in current["decisions"]):
        return
    options = run["options"]
    tokens = token_total(task)
    count = len(active["units"])
    before = current["batch_limit"]
    if options.get("adaptive_policy_version", 1) >= 2:
        _observe_probe(current, options, task, active, seconds, tokens, resource_pressure or [])
        ceiling = max(initial_timeout_seconds(run, job), options["compile_timeout_max_seconds"])
        current["timeout_seconds"] = min(ceiling, max(
            current["timeout_seconds"], math.ceil((seconds * 3 + 300) / 60) * 60))
        current["decisions"][-1]["timeout_seconds"] = current["timeout_seconds"]
        return
    pressure = (unhealthy or str(task.get("status", "")).lower() != "completed"
                or seconds > options["compile_wall_soft_limit_seconds"]
                or (tokens is not None and tokens > options["compile_token_soft_limit"]))
    reason = "hold"
    if pressure:
        current["batch_limit"] = max(1, before // 2)
        current["clean_streak"] = 0
        current["recent"] = []
        reason = "backoff"
    elif tokens is not None:
        current["clean_streak"] += 1
        current["recent"] = (current["recent"] + [{"tokens_per_unit": tokens / count,
                                                   "seconds_per_unit": seconds / count}])[-3:]
        candidate = min(before + 1, options["adaptive_batch_max"])
        recent = current["recent"]
        if (count >= before and candidate > before
                and current["clean_streak"] >= options["adaptive_clean_compiles"]
                and max(v["tokens_per_unit"] for v in recent) * candidate <= options["compile_token_soft_limit"]
                and max(v["seconds_per_unit"] for v in recent) * candidate
                <= options["compile_wall_soft_limit_seconds"]):
            current["batch_limit"] = candidate
            current["clean_streak"] = 0
            reason = "grow_one"
    else:
        current["clean_streak"] = 0
        current["recent"] = []
        reason = "usage_unknown"
    # Never shorten the accepted timeout. Slow completed work informs a bounded increase.
    ceiling = max(initial_timeout_seconds(run, job), options["compile_timeout_max_seconds"])
    current["timeout_seconds"] = min(ceiling, max(
        current["timeout_seconds"], math.ceil((seconds * 3 + 300) / 60) * 60))
    current["decisions"].append({"task_id": active["task_id"], "units": count,
        "tokens": tokens, "seconds": round(seconds, 3), "before": before,
        "after": current["batch_limit"], "timeout_seconds": current["timeout_seconds"], "reason": reason})


def _observe_probe(current, options, task, active, seconds, tokens, resource_pressure):
    """One-unit probes after same-batch observations; never multiply fixed wall costs."""
    before = current["batch_limit"]
    count = len(active["units"])
    causes = list(resource_pressure)
    if seconds > options["compile_wall_soft_limit_seconds"]:
        causes.append("wall_limit")
    if tokens is not None and tokens > options["compile_token_soft_limit"]:
        causes.append("token_limit")
    blocked, reason = [], "hold"
    if causes:
        current.update(batch_limit=max(1, before // 2), clean_streak=0, recent=[])
        reason = "backoff"
    elif active.get("mode") == "field_patch":
        # Repair traffic cannot justify expansion of the ordinary extraction batch.
        reason = "repair_hold"
    elif str(task.get("status", "")).lower() != "completed":
        current.update(clean_streak=0, recent=[])
        blocked.append("remote_failed_without_resource_signal")
    elif tokens is None:
        current.update(clean_streak=0, recent=[])
        reason = "usage_unknown"
        blocked.append("usage_unknown")
    elif count != before:
        blocked.append("batch_not_full")
    else:
        current["clean_streak"] += 1
        current["recent"] = (current["recent"] + [{"units": count, "tokens": tokens, "seconds": seconds}])[-3:]
        observations = [v for v in current["recent"] if v.get("units") == before]
        if current["clean_streak"] < options["adaptive_clean_compiles"] or len(observations) < 3:
            blocked.append("need_three_stable_batches")
        headroom = options["adaptive_growth_headroom_percent"] / 100
        if max(v["tokens"] for v in observations) > options["compile_token_soft_limit"] * headroom:
            blocked.append("token_headroom")
        if max(v["seconds"] for v in observations) > options["compile_wall_soft_limit_seconds"] * headroom:
            blocked.append("wall_headroom")
        if before >= options["adaptive_batch_max"]:
            blocked.append("batch_ceiling")
        if not blocked:
            current.update(batch_limit=before + 1, clean_streak=0, recent=[])
            reason = "grow_one"
    current["decisions"].append({"task_id": active["task_id"], "units": count, "tokens": tokens,
        "seconds": round(seconds, 3), "before": before, "after": current["batch_limit"],
        "reason": reason, "pressure_reasons": sorted(set(causes)), "growth_blocked_by": blocked,
        "mode": active.get("mode", "extract"), "quality_issues": active.get("quality_issues", 0), "policy_version": 2})
