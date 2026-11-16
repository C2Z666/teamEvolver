"""Whitelisted optional OpenViking commit observations."""

import math

PHASES = ("directory_finalize", "directory_prepare", "directory_index_wait", "directory_tag_visibility",
          "output_lock", "snapshot", "diff", "versions", "conflicts")


def commit_timings(task):
    raw = (task.get("meta") or {}).get("compile_commit_timings")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in PHASES:
        row = raw.get(key)
        if not isinstance(row, dict):
            continue
        seconds, calls = row.get("seconds"), row.get("calls")
        if (type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0
                or type(calls) is not int or calls < 1):
            continue
        result[key] = {"seconds": seconds, "calls": calls,
                       "last_outcome": "completed" if row.get("last_outcome") == "completed" else "interrupted"}
    return result
