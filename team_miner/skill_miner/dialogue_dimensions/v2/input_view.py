"""Lossless compact model views and bounded, hashed stage assignments."""

from ..input import BUSINESS, canonical, normalize_case, sha
from . import PROTOCOL

DEFAULTS = {
    "seed_cases_per_point": 2, "seed_case_limit": 54, "propose_items": 8, "proposals_per_packet": 3,
    "match_items": 8, "candidate_top_k": 5, "expanded_candidate_limit": 12,
    "register_page_dimensions": 6, "audit_dimensions": 4, "representative_spans": 6,
    "packet_input_chars": 12000, "packet_input_bytes": 48000, "packets_per_compile": 4,
    "compile_input_chars": 32000, "max_inflight_compiles": 1, "novelty_rounds": 2,
    "revision_rounds": 1, "repair_attempts_per_unit": 2, "max_model_units": 1500,
}


def model_view(value):
    case = normalize_case(value)
    raw, cursor, fragments = case["dialogue_text"], 0, []
    for sentence in case["sentence_index"]:
        start, end = sentence["char_start"], sentence["char_end"]
        if start > cursor:
            fragments.append({"text": raw[cursor:start]})
        fragments.append({"sentence_id": sentence["sentence_id"], "speaker": sentence["speaker"],
                          "text": raw[start:end]})
        cursor = end
    if cursor < len(raw):
        fragments.append({"text": raw[cursor:]})
    assert "".join(item["text"] for item in fragments) == raw
    view = {"schema_version": "taikang.dialogue-model.v2", "renderer_version": 2,
            **{key: case[key] for key in ("case_ref", "case_version", *BUSINESS[:-1])}, "fragments": fragments}
    return {**view, "content_hash": sha(canonical(view))}


def packet(stage, payload, revision, ordinal, options=None):
    from .schemas import RESULTS, Receipt
    limits = options or DEFAULTS
    identifier = f"unit-{ordinal:06d}"
    value = {"schema_version": "taikang.dimension-task.v2", "execution_protocol": PROTOCOL,
             "unit_id": identifier, "stage": stage, "registry_revision": revision,
             "output_path": f"analysis/dialogue_dimensions/receipts/{identifier}.json", "payload": payload,
             "result_schema": RESULTS[stage].model_json_schema(),
             "receipt_schema": Receipt.model_json_schema()}
    value["input_hash"] = sha(canonical(value))
    text = canonical(value)
    if len(text) > limits["packet_input_chars"] or len(text.encode()) > limits["packet_input_bytes"]:
        raise ValueError("capacity_pending")
    return value
