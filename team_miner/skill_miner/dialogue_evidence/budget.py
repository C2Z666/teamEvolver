"""Deterministic lossless units and conservative bounded split plans."""

from ..dialogue_dimensions.input import BUSINESS, canonical, sha
from ..dialogue_dimensions.v2.input_view import model_view
from . import PROTOCOL

DEFAULTS = {
    "packets_per_compile": 4, "packet_input_chars": 12000, "packet_input_bytes": 48000,
    "compile_input_chars": 32000, "max_inflight_compiles": 1,
    "repair_attempts_per_unit": 2, "transport_retry_attempts": 5,
    "transport_retry_delay_seconds": 60, "capacity_split_depth": 3,
    "field_repair_attempts_per_unit": 1, "adaptive_batching": 1, "adaptive_batch_max": 6,
    "adaptive_clean_compiles": 3, "compile_token_soft_limit": 300000,
    "compile_wall_soft_limit_seconds": 900, "compile_timeout_max_seconds": 7200,
    "job_deadline_seconds": 172800,
}


def packet(case, owned, ordinal, *, index=1, count=1, adjacent_turns=True):
    sentences = case["sentence_index"]
    all_ids = [s["sentence_id"] for s in sentences]
    full = owned == all_ids
    visible = set(owned)
    if owned and not full:
        first, last = all_ids.index(owned[0]), all_ids.index(owned[-1])
        # Adjacent full turns where possible; no summaries or coordinate rewriting.
        turns = {sentences[max(0, first - 1)]["turn_index"], sentences[min(len(sentences)-1, last+1)]["turn_index"]}
        if adjacent_turns:
            visible.update(s["sentence_id"] for s in sentences if s["turn_index"] in turns)
        else:
            visible.update(all_ids[max(0, first-1):min(len(all_ids), last+2)])
    fragments = model_view(case)["fragments"]
    if not full:
        positions = [i for i, f in enumerate(fragments) if f.get("sentence_id") in visible]
        fragments = fragments[min(positions):max(positions)+1]
        visible = {f["sentence_id"] for f in fragments if "sentence_id" in f}
    identifier = f"unit-{ordinal:06d}"
    value = {"schema_version": "taikang.evidence-task.v1", "execution_protocol": PROTOCOL,
             "unit_id": identifier, "stage": "extract", "output_path": identifier + ".json",
             "case_ref": case["case_ref"], "case_version": case["case_version"],
             "payload": {"metadata": {k: case[k] for k in BUSINESS[:-1]}, "fragments": fragments,
                         "scope": {"kind": "full_case" if full else "segment", "owned_sentence_ids": owned,
                                   "context_sentence_ids": [s for s in all_ids if s in visible and s not in owned],
                                   "segment_index": index, "segment_count": count}}}
    value["input_hash"] = sha(canonical(value))
    return value


def fits(value, options):
    text = canonical(value)
    return (len(text) <= min(options["packet_input_chars"], options["compile_input_chars"])
            and len(text.encode()) <= options["packet_input_bytes"])


def divide(case, owned):
    if len(owned) < 2:
        return []
    positions = {s["sentence_id"]: s["turn_index"] for s in case["sentence_index"]}
    boundaries = [i for i in range(1, len(owned)) if positions[owned[i-1]] != positions[owned[i]]]
    middle = min(boundaries, key=lambda i: abs(i-len(owned)/2)) if boundaries else len(owned)//2
    return [owned[:middle], owned[middle:]]


def plan(case, options, ordinal=1, owned=None, depth=0):
    owned = [s["sentence_id"] for s in case["sentence_index"]] if owned is None else owned
    value = packet(case, owned, ordinal)
    if not fits(value, options):
        value = packet(case, owned, ordinal, adjacent_turns=False)
    if fits(value, options):
        return [{"packet": value, "status": "planned", "attempt": 0, "depth": depth}]
    children = divide(case, owned)
    if not children:
        return [{"packet": value, "status": "failed", "attempt": 0, "depth": depth,
                 "error": "capacity_exceeded"}]
    result = []
    for child in children:
        result.extend(plan(case, options, ordinal+len(result), child, depth))
    return result
