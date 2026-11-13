"""Persisted, bounded batch tuning using completed Compile consumption only."""

import math


def policy(run, job):
    options = run["options"]
    if not options.get("adaptive_batching", 0):
        return None
    return run.setdefault("adaptive", {
        "batch_limit": options["packets_per_compile"], "clean_streak": 0, "recent": [],
        "timeout_seconds": job["runtime_timeout_seconds"], "decisions": [],
    })


def batch_limit(run, job):
    current = policy(run, job)
    return current["batch_limit"] if current else run["options"]["packets_per_compile"]


def timeout_seconds(run, job):
    current = policy(run, job)
    return current["timeout_seconds"] if current else job["runtime_timeout_seconds"]


def token_total(task):
    tokens = (task.get("meta") or {}).get("pipeline", {}).get("tokens")
    if not isinstance(tokens, dict):
        return None
    values = [tokens.get(k) for k in ("input", "output")]
    if any(type(v) is not int or v < 0 for v in values):
        return None
    return sum(values) or None


def observe(run, job, *, task, active, seconds, unhealthy=False):
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
    ceiling = max(job["runtime_timeout_seconds"], options["compile_timeout_max_seconds"])
    current["timeout_seconds"] = min(ceiling, max(
        current["timeout_seconds"], math.ceil((seconds * 3 + 300) / 60) * 60))
    current["decisions"].append({"task_id": active["task_id"], "units": count,
        "tokens": tokens, "seconds": round(seconds, 3), "before": before,
        "after": current["batch_limit"], "timeout_seconds": current["timeout_seconds"], "reason": reason})
