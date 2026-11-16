"""Patches cannot rewrite accepted content, identity or invalid temporal references."""

import copy

import pytest

from team_miner.skill_miner.default_compiler import SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical, sha
from team_miner.skill_miner.dialogue_evidence import adaptive, patches
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS
from team_miner.skill_miner.dialogue_evidence.compiler import repair_body, sections, stage_body
from team_miner.skill_miner.dialogue_evidence.validation import validate
from tests.support.dialogue_evidence import receipt, task


def repair_fixture():
    packet = task()
    raw = receipt(packet)
    raw["result"]["evidence"][0]["trigger"] = {
        "text": "事后回应", "sentence_refs": ["s000002"], "metadata_refs": []}
    text = canonical(raw)
    unit = {"packet": packet, "fallback": {"raw_text": text, "receipt_hash": sha(text),
                                           "receipt": validate(text, packet)}}
    request = patches.request(unit)
    patch = {"schema_version": "taikang.evidence-field-patch.v1",
        **{k: request[k] for k in (*patches.IDENTITY, "base_receipt_hash", "repair_hash")},
        "patches": [{"local_ref": "e1", "field": "trigger",
                     "value": {"text": "", "sentence_refs": [], "metadata_refs": []}}]}
    return unit, raw, patch


def test_patch_preserves_all_other_fields_and_revalidates():
    unit, original, patch = repair_fixture()
    raw, checked = patches.merge(canonical(patch), unit)
    expected = copy.deepcopy(original)
    expected["result"]["evidence"][0]["trigger"] = patch["patches"][0]["value"]
    assert raw == canonical(expected)
    assert not checked["quarantined"]
    assert unit["fallback"]["raw_text"] == canonical(original)


@pytest.mark.parametrize("bad", ["identity", "base", "unassigned", "missing", "duplicate", "late_trigger"])
def test_patch_rejects_wrong_identity_scope_or_remaining_defect(bad):
    unit, _, patch = repair_fixture()
    if bad == "identity":
        patch["case_version"] = "foreign"
    elif bad == "base":
        patch["base_receipt_hash"] = "foreign"
    elif bad == "unassigned":
        patch["patches"][0]["field"] = "scenario"
    elif bad == "missing":
        patch["patches"] = []
    elif bad == "duplicate":
        patch["patches"] *= 2
    else:
        patch["patches"][0]["value"] = {
            "text": "仍为事后回应", "sentence_refs": ["s000002"], "metadata_refs": []}
    with pytest.raises(ValueError):
        patches.merge(canonical(patch), unit)


def test_old_skill_still_slices_and_patch_skill_has_no_full_extraction_contract():
    old = SKILL_BODY.split("<!-- s3:repair:start -->")[0]
    assert list(sections(old)[1]) == ["common", "transport", "stage:extract"]
    assert "Required receipt schema" in stage_body(old, "extract")
    assert "Required patch schema" in repair_body(SKILL_BODY)
    assert "Required receipt schema" not in repair_body(SKILL_BODY)
    with pytest.raises(ValueError, match="no_patch_contract"):
        repair_body(old)


def probe(run, i, *, seconds=490, pressure=None, mode="extract", tokens=120000):
    job = {"runtime_timeout_seconds": 3600}
    active = {"task_id": str(i), "units": list(range(adaptive.batch_limit(run, job))), "mode": mode}
    adaptive.observe(run, job, task={"status": "completed", "meta": {"pipeline": {
        "tokens": {"input": tokens, "output": 1000}}}}, active=active, seconds=seconds,
        unhealthy=True, resource_pressure=pressure)


def test_quality_issues_do_not_shrink_batches_or_block_bounded_recovery_from_one():
    run = {"options": dict(DEFAULTS)}
    probe(run, 0, pressure=["rate_limit"])
    assert run["adaptive"]["batch_limit"] == 1
    for i in range(1, 4):
        probe(run, i)
    # 490 * 2 exceeded the old 900s projection; a one-unit probe now has 25% headroom.
    assert run["adaptive"]["batch_limit"] == 2
    for i in range(4, 20):
        probe(run, i)
    assert run["adaptive"]["batch_limit"] == 4
    before = copy.deepcopy(run)
    probe(run, 19)
    assert run == before


def test_repairs_cannot_grow_normal_batch_and_limits_still_back_off():
    run = {"options": dict(DEFAULTS)}
    for i in range(4):
        probe(run, i, mode="field_patch")
    assert run["adaptive"]["batch_limit"] == DEFAULTS["packets_per_compile"]
    probe(run, 4, tokens=400000)
    assert run["adaptive"]["batch_limit"] == 1
    assert "token_limit" in run["adaptive"]["decisions"][-1]["pressure_reasons"]


def test_growth_reports_actual_headroom_blocker():
    run = {"options": dict(DEFAULTS)}
    for i in range(5):
        probe(run, i, seconds=700)
    assert run["adaptive"]["batch_limit"] == DEFAULTS["packets_per_compile"]
    assert "wall_headroom" in run["adaptive"]["decisions"][-1]["growth_blocked_by"]
