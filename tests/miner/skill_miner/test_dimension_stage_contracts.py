import copy

import pytest

from team_miner.skill_miner.dialogue_dimensions.input import canonical, sha
from team_miner.skill_miner.dialogue_dimensions.v2.compiler import STAGES, sections, stage_body
from team_miner.skill_miner.dialogue_dimensions.v2.input_view import DEFAULTS, model_view, packet
from team_miner.skill_miner.dialogue_dimensions.v2.registry import consume, register
from team_miner.skill_miner.dialogue_dimensions.v2.resource import SHA256, SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.v2.validation import validate
from team_miner.skill_miner.settings import reset_prompt, settings_payload, update_settings
from tests.support.dialogue_dimensions_v2 import card, case, dimension, observation, receipt


@pytest.mark.parametrize("stage", STAGES)
def test_slices_use_frozen_body_only(stage):
    custom = SKILL_BODY.replace("共同规则", "自定义共同规则")
    sliced = stage_body(custom, stage)
    assert "自定义共同规则" in sliced
    assert "## stage=" + stage in sliced
    assert sum("## stage=" + other in sliced for other in STAGES) == 1
    assert sha(custom) != SHA256


@pytest.mark.parametrize("body", [SKILL_BODY.replace("<!-- s2:common:start -->", ""),
                                  SKILL_BODY + "<!-- s2:common:start -->",
                                  SKILL_BODY + "\n请保留这条自定义规则"])
def test_malformed_sections_rejected(body):
    with pytest.raises(ValueError):
        sections(body)
    with pytest.raises(ValueError):
        update_settings({}, {"prompts": [{"id": "ov_compile", "prompt": body}]})


def test_default_reset_and_effective_snapshot_agree():
    from team_miner.skill_miner.default_compiler import SHA256, SKILL_BODY
    value = {"mining": {"other": "preserved", "s2_migration": "marker"}}
    payload = settings_payload(value)
    assert payload["candidate_mode"] == "none"
    assert not payload["candidate_mode_warning"]
    assert payload["default_sha256"] == payload["effective_sha256"] == SHA256
    update_settings(value, {"prompts": [{"id": "ov_compile", "prompt": SKILL_BODY.replace("共同规则", "规则定制")}]})
    assert settings_payload(value)["prompts"][0]["overridden"]
    assert reset_prompt(value, "ov_compile")["prompts"][0]["effective_prompt"] == SKILL_BODY
    assert value["mining"]["other"] == "preserved"
    assert value["mining"]["s2_migration"] == "marker"


@pytest.mark.parametrize("text", [
    "HWP：你好。\nC：谢谢。\n", "未知前缀\nHWP:目标 123.4\n\nC:三年", "  \n没有角色！尾部"])
def test_model_view_reconstructs_every_character(text):
    view = model_view(case(text))
    assert "".join(row["text"] for row in view["fragments"]) == text
    assert "sentence_index" not in view


def test_extract_wrong_role_is_isolated_and_foreign_ids_block():
    task = packet("extract", {"case": model_view(case())}, None, 1)
    row = observation()
    row["action_refs"] = ["s000002"]
    accepted = validate(canonical(receipt(task, {"observations": [row], "empty_reason": ""})), task)
    assert not accepted["result"]["observations"]
    assert accepted["result"]["isolated"]
    row["action_refs"] = ["s999999"]
    with pytest.raises(ValueError, match="foreign_source"):
        validate(canonical(receipt(task, {"observations": [row], "empty_reason": ""})), task)


def test_zero_findings_and_empty_reason():
    task = packet("extract", {"case": model_view(case())}, None, 1)
    assert validate(canonical(receipt(task, {"observations": [], "empty_reason": "普通问候"})), task)
    with pytest.raises(ValueError, match="empty_reason"):
        validate(canonical(receipt(task, {"observations": [], "empty_reason": ""})), task)


def test_map_receipt_contract_includes_envelope_and_issue_reason():
    task = packet("extract", {"case": model_view(case())}, None, 1)
    schema = task["receipt_schema"]
    assert "schema_version" in schema["required"]
    assert "reason" in schema["$defs"]["Issue"]["required"]
    malformed = receipt(task, {"observations": [], "empty_reason": "普通问候"})
    del malformed["schema_version"]
    with pytest.raises(ValueError, match="schema_version"):
        validate(canonical(malformed), task)


@pytest.mark.parametrize("field,value", [("input_hash", "foreign"), ("registry_revision", 2), ("unit_id", "foreign")])
def test_envelope_identity_not_repaired(field, value):
    task = packet("extract", {"case": model_view(case())}, None, 1)
    result = receipt(task, {"observations": [], "empty_reason": "无"})
    result[field] = value
    with pytest.raises(ValueError, match="identity_mismatch"):
        validate(canonical(result), task)


def test_match_requires_every_member_and_candidate_version():
    task = packet("match", {"evidence": [{"evidence_id": "ev-1"}], "dimensions": [dimension()],
                            "allowed_candidates": {"ev-1": ["dim-1"]}}, 1, 2)
    with pytest.raises(ValueError, match="coverage"):
        validate(canonical(receipt(task, {"decisions": []})), task)
    row = {"evidence_id": "ev-1", "action": "attach", "targets": [
        {"dimension_id": "dim-1", "version": 2, "relation": "same_dimension"}], "method_text": "", "reason": "相同"}
    with pytest.raises(ValueError, match="version"):
        validate(canonical(receipt(task, {"decisions": [row]})), task)


@pytest.mark.parametrize("signal,applicable,sufficient,valid", [
    ("na", "no", "yes", True), ("ne", "unknown", "yes", True), ("ne", "yes", "no", True),
    ("not_demonstrated", "yes", "yes", False), ("support", "no", "yes", False), ("mixed", "yes", "yes", False),
    ("mixed", "yes", "no", False), ("na", "unknown", "yes", False),
])
def test_audit_applicability_and_evidence(signal, applicable, sufficient, valid):
    task = packet("audit", {"case": model_view(case()), "dimensions": [dimension()]}, 1, 3)
    row = {"dimension_id": "dim-1", "version": 1, "applicable": applicable, "sufficient": sufficient,
           "signal": signal, "support_refs": [], "counter_refs": [], "reason": "检查片段"}
    text = canonical(receipt(task, {"assessments": [row], "uncovered": []}))
    if valid:
        assert validate(text, task)
    else:
        with pytest.raises(ValueError) as error:
            validate(text, task)
        if applicable == "unknown" or sufficient == "no":
            assert "dim-1" in str(error.value)
            assert "requires signal=ne" in str(error.value)


def test_packet_capacity_checks_bytes_without_truncating():
    value = case("HWP：" + "原" * 5000)
    options = {**DEFAULTS, "packet_input_bytes": 10000}
    with pytest.raises(ValueError, match="capacity_pending"):
        packet("extract", {"case": model_view(value)}, None, 1, options)
    assert len(value["dialogue_text"]) == 5004


def registry_run():
    return {"dimensions": {"dim-1": dimension()}, "metrics": {"register_reused": 0, "register_created": 0},
            "registry_revision": 1, "versions": [], "changed": [], "lineage": [], "evidence": {},
            "issues": [], "memberships": [], "dispositions": {}}


def test_registration_missing_page_cannot_create_and_same_target_reuses():
    run = registry_run()
    proposal = {"proposal_id": "p1", "card": card(), "member_refs": []}
    register(run, proposal, [])
    assert proposal["status"] == "deferred"
    register(run, proposal, [{"dimension_id": "dim-1", "version": 1, "relation": "same_dimension"}])
    assert proposal["dimension_id"] == "dim-1"
    assert len(run["dimensions"]) == 1
    assert run["metrics"]["register_reused"] == 1


def test_revision_keeps_identity_but_invalidates_old_audit_version():
    run = registry_run()
    proposal = {"proposal_id": "p1", "card": {**card(), "applicability": "新的表现机会"}, "member_refs": [],
                "replace_sources": ["dim-1"], "canonical_id": "dim-1", "revision": True}
    register(run, proposal, [])
    assert run["dimensions"]["dim-1"]["version"] == 2
    assert run["changed"] == ["dim-1"]
    assert run["registry_revision"] == 2
    assert run["metrics"]["register_created"] == 0
    assert run["metrics"]["register_updated"] == 1


def test_stale_receipt_never_mutates_registry():
    run = registry_run()
    before = copy.deepcopy(run)
    task = packet("register", {"dimensions": [dimension()]}, 0, 1)
    with pytest.raises(ValueError, match="stale_registry"):
        consume(run, {"packet": task}, receipt(task, {"comparisons": []}))
    assert run == before


def test_partial_split_retains_parent_and_defers_partial_children():
    from team_miner.skill_miner.dialogue_dimensions.v2.registry import finalize_splits

    run = registry_run()
    first = {"proposal_id": "p1", "card": card(), "member_refs": [], "replace_sources": ["dim-1"],
             "revision": True, "split_group": "split-one"}
    second = {**first, "proposal_id": "p2", "status": "deferred"}
    run["proposals"] = [first, second]
    register(run, first, [])
    assert run["metrics"]["register_created"] == 1
    assert run["dimensions"]["dim-1"]["status"] == "active"
    finalize_splits(run)
    assert run["dimensions"]["dim-1"]["status"] == "active"
    assert run["dimensions"][first["dimension_id"]]["status"] == "deferred"
    assert not run["lineage"]
    assert not run["changed"]
    revision = run["registry_revision"]
    finalize_splits(run)
    assert run["registry_revision"] == revision


def test_audit_customer_response_alone_cannot_support_hwp_ability():
    task = packet("audit", {"case": model_view(case()), "dimensions": [dimension()]}, 1, 3)
    row = {"dimension_id": "dim-1", "version": 1, "applicable": "yes", "sufficient": "yes",
           "signal": "support", "support_refs": ["s000002"], "counter_refs": [], "reason": "客户同意"}
    with pytest.raises(ValueError, match="support_requires_hwp_behavior"):
        validate(canonical(receipt(task, {"assessments": [row], "uncovered": []})), task)
    row["support_refs"].append("s000001")
    assert validate(canonical(receipt(task, {"assessments": [row], "uncovered": []})), task)
