import copy

import pytest

from team_miner.skill_miner.compiler_protocol import detect
from team_miner.skill_miner.default_compiler import PROTOCOL, SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical, sha
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan
from team_miner.skill_miner.dialogue_evidence.compiler import sections, stage_body
from team_miner.skill_miner.dialogue_evidence.schemas import Task
from team_miner.skill_miner.dialogue_evidence.validation import validate
from tests.support.dialogue_evidence import case, evidence, receipt, task


def test_default_and_executable_contract():
    assert detect(SKILL_BODY) == PROTOCOL == "te-dialogue-evidence.v1"
    assert list(sections(SKILL_BODY)[1]) == ["common", "transport", "stage:extract"]
    body = stage_body(SKILL_BODY, "extract")
    assert 'taikang.evidence-result.v1' in body and 'needs_pipeline=true' in body
    assert 'registry_revision' not in body
    with pytest.raises(ValueError):
        detect(SKILL_BODY + "\nte-dialogue-dimensions.v2")
    with pytest.raises(ValueError):
        detect("te-dialogue-evidence.v9")
    with pytest.raises(ValueError):
        sections(SKILL_BODY + "\n丢失的自定义规则")


@pytest.mark.parametrize("text", ["HWP：繁體😀，ASR。。。\nC：嗯？\r\n", "没有已知角色", "HWP：你好。"])
def test_lossless_task_and_hash(text):
    unit = plan(case(text), DEFAULTS)[0]["packet"]
    Task.model_validate(unit)
    assert ''.join(f["text"] for f in unit["payload"]["fragments"]) == text
    value = dict(unit)
    digest = value.pop("input_hash")
    assert digest == sha(canonical(value))


@pytest.mark.parametrize("field", ["case_ref", "case_version", "input_hash", "unit_id"])
def test_foreign_receipt_never_consumed(field):
    packet = task()
    value = receipt(packet)
    value[field] = "foreign"
    with pytest.raises(ValueError, match="identity"):
        validate(canonical(value), packet)


@pytest.mark.parametrize("refs", [["s000002"], ["s999999"], []])
def test_invalid_action_isolated_without_losing_other_evidence(refs):
    packet = task()
    bad = {**evidence(), "local_ref": "e2", "action_refs": refs}
    value = validate(canonical(receipt(packet, [evidence(), bad])), packet)
    assert len(value["result"]["evidence"]) == len(value["quarantined"]) == 1


def test_bad_fact_and_late_trigger_isolated_without_fabricating_facts():
    packet = task()
    row = evidence()
    row["observed_conditions"] = [{"text": "编造", "sentence_refs": ["foreign"], "metadata_refs": []}]
    row["trigger"] = {"text": "事后回应", "sentence_refs": ["s000002"], "metadata_refs": []}
    result = validate(canonical(receipt(packet, [row])), packet)
    assert result["result"]["evidence"][0]["observed_conditions"] == []
    assert result["result"]["evidence"][0]["trigger"]["text"] == ""
    assert len(result["quarantined"]) == 2


def test_zero_and_capacity_are_distinct_and_truncation_never_consumed():
    packet = task()
    value = receipt(packet, [])
    assert validate(canonical(value), packet)["result"]["evidence"] == []
    value.update(status="needs_split", issues=[{"reason": "output_capacity", "refs": [], "detail": "太长"}])
    with pytest.raises(ValueError, match="capacity"):
        validate(canonical(value), packet)
    with pytest.raises(ValueError):
        validate(canonical(value)[:-10], packet)


def test_split_coverage_is_disjoint_and_bounded():
    source = case("\n".join("HWP：" + "方法"*20 + "。" for _ in range(30)))
    units = plan(source, {**DEFAULTS, "packet_input_chars": 2000})
    owned = [s for u in units for s in u["packet"]["payload"]["scope"]["owned_sentence_ids"]]
    assert len(owned) == len(set(owned)) == len(source["sentence_index"])
    assert len(units) > 1
    for unit in units:
        scope = unit["packet"]["payload"]["scope"]
        assert not set(scope["owned_sentence_ids"]) & set(scope["context_sentence_ids"])


def test_context_only_action_rejected():
    packet = copy.deepcopy(task())
    packet["payload"]["scope"]["owned_sentence_ids"] = ["s000003"]
    assert validate(canonical(receipt(packet)), packet)["quarantined"]


def test_long_single_turn_splits_by_sentence_without_repeating_entire_turn():
    source = case("HWP：" + "原文方法说明。"*150)
    units = plan(source, {**DEFAULTS, "packet_input_chars": 1800})
    assert len(units) > 1
    assert all(u["status"] == "planned" for u in units)
    assert all(len(canonical(u["packet"])) <= 1800 for u in units)
