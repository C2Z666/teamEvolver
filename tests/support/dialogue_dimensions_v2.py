"""Synthetic sources and isolated persistence for dimension workflow tests."""

import copy

from team_miner.skill_miner.dialogue_dimensions.input import normalize_case


def case(text="HWP：您希望什么时候完成目标？\nC：三年后。\nHWP：目前准备了多少？"):
    return normalize_case({"case_ref": "case_" + "a" * 24, "course": "规划", "chapter": "需求",
                           "knowledge_point": "目标", "scene": "初次沟通", "dialogue_text": text})


def card():
    return {"name": "目标澄清", "question": "是否澄清目标", "definition": "确认客户目标及时间",
            "applicability": "客户有目标时",
            "boundaries": ["不评价成交"], "indicators": ["询问并确认"], "scope": "core"}


def dimension(identifier="dim-1", version=1):
    return {"dimension_id": identifier, "version": version, "card": card(), "status": "active"}


def observation():
    return {"local_ref": "o1", "behavior": "询问目标时间", "evaluation_question": "是否澄清目标",
            "trigger_refs": [], "action_refs": ["s000001"], "response_refs": ["s000002"],
            "value_status": "support_candidate", "limits": [], "business_fact_status": "not_involved"}


def receipt(task, result):
    return {"schema_version": "taikang.dimension-result.v2", **{key: task[key] for key in
            ("unit_id", "stage", "input_hash", "registry_revision")}, "status": "analyzed",
            "result": result, "issues": []}


class MemoryRepository:
    def __init__(self):
        self.records = {}

    async def get(self, key):
        return copy.deepcopy(self.records.get(key))

    async def save(self, run, **records):
        self.records.update(copy.deepcopy(records))
        self.records["run"] = copy.deepcopy(run)


def run_state():
    from team_miner.skill_miner.default_compiler import SHA256
    from team_miner.skill_miner.dialogue_dimensions.input import case_key
    from team_miner.skill_miner.dialogue_dimensions.v2.input_view import DEFAULTS
    raw = case()
    return {"skill_hash": SHA256, "dataset_hash": "frozen", "embedding_model": "model-1", "options": dict(DEFAULTS),
            "stage": "extract", "planned": False, "queue": [], "planned_packets": {}, "active": None,
            "unit_count": 0, "registry_revision": 0, "cases": {case_key(raw): raw}, "extracted": {},
            "evidence": {}, "dispositions": {}, "proposals": [], "dimensions": {}, "versions": [],
            "memberships": [], "audits": [], "rubrics": {}, "gaps": [], "issues": [], "lineage": [],
            "changed": [], "novelty_round": 0, "metrics": {"compiles": 0, "accepted": 0, "retries": 0,
            "register_reused": 0, "register_created": 0, "stage_units": {}, "peak_packet_chars": 0}}
