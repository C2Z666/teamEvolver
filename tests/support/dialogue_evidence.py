"""Constructed Evidence fixtures, unrelated to customer data."""

from team_miner.skill_miner.dialogue_dimensions.input import normalize_case
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan


def case(text="HWP：先确认您方便吗？\nC：可以。\nHWP：我们约十分钟。"):
    return normalize_case({"case_ref": "case_" + "a" * 24, "dialogue_text": text})


def task():
    return plan(case(), DEFAULTS)[0]["packet"]


def evidence():
    empty = {"text": "", "sentence_refs": [], "metadata_refs": []}
    return {"local_ref": "e1", "behavior": "先确认交流是否方便", "action_refs": ["s000001"],
            "scenario": empty.copy(), "trigger": empty.copy(), "observed_conditions": [],
            "suggested_applicability": [], "response_refs": ["s000002"], "uncertainties": []}


def receipt(packet, rows=None):
    rows = [evidence()] if rows is None else rows
    return {"schema_version": "taikang.evidence-result.v1",
            **{k: packet[k] for k in ("unit_id", "stage", "input_hash", "case_ref", "case_version")},
            "status": "analyzed", "result": {"evidence": rows, "empty_reason": "纯寒暄" if not rows else ""},
            "issues": []}
