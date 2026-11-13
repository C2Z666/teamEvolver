import copy

import pytest

from team_miner.skill_miner.dialogue_dimensions.input import case_key
from team_miner.skill_miner.dialogue_dimensions.v2 import audit_gaps, evidence_context
from team_miner.skill_miner.dialogue_dimensions.v2 import orchestration as flow
from team_miner.skill_miner.dialogue_dimensions.v2.registry import consume
from tests.support.dialogue_dimensions_v2 import MemoryRepository, dimension, observation, run_state


def material(run, identifier, action="s000001", response="s000002", status="support_candidate"):
    raw = next(iter(run["cases"].values()))
    row = {**observation(), "case_ref": raw["case_ref"], "case_version": raw["case_version"],
           "evidence_id": identifier, "member_ref": identifier, "action_refs": [action],
           "response_refs": [response] if response else [], "value_status": status}
    run["evidence"][identifier] = row
    return row


def gap(run, member="gap-local"):
    raw = next(iter(run["cases"].values()))
    return {"case_ref": raw["case_ref"], "case_version": raw["case_version"], "member_ref": member,
            "sentence_refs": ["s000001"], "issue": "当前页没有目标澄清", "suggested_question": "是否澄清目标"}


def test_same_case_evidence_uses_its_own_actions_and_keeps_speaker():
    run = run_state()
    first = material(run, "first")
    second = material(run, "second", "s000003", "")
    payload = evidence_context.ground(run, {"evidence": [first, second]})
    one, two = payload["evidence"]
    assert set(one["source_span_refs"]).isdisjoint(two["source_span_refs"])
    sources = {s["span_ref"]: s for s in payload["spans"]}
    assert sources[two["action_refs"][0]]["text"] == "目前准备了多少？"
    assert sources[one["response_refs"][0]]["speaker"] == "C"
    assert all(sources[r]["speaker"] == "HWP" for r in one["action_refs"] + two["action_refs"])


@pytest.mark.asyncio
async def test_bounded_packet_preserves_actions_both_signals_and_records_omissions():
    run, repo = run_state(), MemoryRepository()
    run["stage"] = "rubric"
    evidence = [material(run, f"e-{i}", status="mixed" if i % 2 else "support_candidate") for i in range(20)]
    for row in evidence:
        row["behavior"] += "较长的完整行为描述。" * 40
    raw = next(iter(run["cases"].values()))
    audit = {"case_ref": raw["case_ref"], "case_version": raw["case_version"], "dimension_id": "dim-1",
             "version": 1, "signal": "counter", "support_refs": [], "counter_refs": ["s000002"],
             "reason": "客户回应及前后行为需要一起判断", "applicable": "yes", "sufficient": "yes"}
    await flow.enqueue(repo, run, "rubric", {"dimension": dimension(), "evidence": evidence,
        "audit_examples": [audit], "methods": [], "scale": [0, 1, 2, 3]})
    assert len(run["queue"]) == 1
    payload = repo.records[run["queue"][0]]["packet"]["payload"]
    assert {e["value_status"] for e in payload["evidence"]} == {"mixed", "support_candidate"}
    assert payload["audit_examples"]
    assert all(e["action_refs"] and e["source_span_refs"] for e in payload["evidence"])
    assert run["representative_selections"][0]["omitted"]
    assert len(run["evidence"]) == 20


@pytest.mark.asyncio
async def test_proposal_split_rebuilds_exact_context_without_foreign_case_material(monkeypatch):
    run, repo = run_state(), MemoryRepository()
    run["stage"] = "bootstrap"
    first = material(run, "first")
    second = material(run, "second", "s000003", "")
    original = flow.packet

    def one(stage, payload, *args):
        if len(payload["evidence"]) > 1:
            raise ValueError("capacity_pending")
        return original(stage, payload, *args)

    monkeypatch.setattr(flow, "packet", one)
    await flow.enqueue(repo, run, "propose", {"evidence": [first, second], "dimensions": []}, "evidence")
    assert len(run["queue"]) == 2
    for unit in run["queue"]:
        payload = repo.records[unit]["packet"]["payload"]
        assert {s["span_ref"] for s in payload["spans"]} == set(payload["evidence"][0]["source_span_refs"])


@pytest.mark.asyncio
async def test_oversized_whole_evidence_is_pending_not_truncated():
    run, repo = run_state(), MemoryRepository()
    row = material(run, "large")
    row["behavior"] = "完整证据" * 4000
    await flow.enqueue(repo, run, "rubric", {"dimension": dimension(), "evidence": [row], "scale": [0, 1, 2, 3]})
    assert not run["queue"]
    assert run["issues"][0]["reason"] == "capacity_pending"
    assert run["evidence"]["large"]["behavior"] == "完整证据" * 4000


def test_gap_requires_all_registry_pages_and_does_not_duplicate_existing_dimension():
    run = run_state()
    run["dimensions"] = {f"dim-{i}": dimension(f"dim-{i}") for i in range(7)}
    run["gaps"] = [gap(run), gap(run, "another-local-finding")]
    audit_gaps.consolidate(run)
    assert len(run["gaps"]) == 1
    g = run["gaps"][0]
    comparisons = [{"dimension_id": f"dim-{i}", "version": 1, "relation": "different", "reason": "另一目标"}
                   for i in range(6)]
    run["gap_comparisons"][g["member_ref"]] = comparisons
    audit_gaps.finalize(run)
    assert g["status"] == "review_incomplete"
    comparisons.append({"dimension_id": "dim-6", "version": 1, "relation": "same_dimension", "reason": "已有覆盖"})
    audit_gaps.finalize(run)
    assert g["status"] == "covered"
    assert g["covered_by"] == ["dim-6"]
    audit_gaps.consolidate(run)
    assert run["gaps"][0]["status"] == "covered"
    assert len(run["gap_comparisons"][g["member_ref"]]) == 7


@pytest.mark.asyncio
async def test_all_confirmed_gaps_get_independent_revision_packets():
    run, repo = run_state(), MemoryRepository()
    run["stage"] = "revise"
    run["gaps"] = [{**gap(run, f"gap-{i}"), "status": "new_candidate"} for i in range(9)]
    await flow.plan(None, None, repo, run)
    assert len(run["queue"]) == 9
    members = set()
    for unit in run["queue"]:
        p = repo.records[unit]["packet"]["payload"]
        assert p["dimensions"] == [] and p["spans"]
        assert p["audit_gaps"][0]["source_span_refs"]
        members.add(p["audit_gaps"][0]["member_ref"])
    assert members == {f"gap-{i}" for i in range(9)}


@pytest.mark.asyncio
async def test_uncertain_evidence_is_in_expanded_match_and_final_coverage():
    run, repo = run_state(), MemoryRepository()
    row = material(run, "uncertain")
    run["stage"] = "expanded_match"
    run["dispositions"][row["evidence_id"]] = "uncertain"
    assert flow.unresolved(run) == [row]
    await flow.plan(None, None, repo, run)
    assert len(run["queue"]) == 1
    assert repo.records[run["queue"][0]]["packet"]["payload"]["evidence"][0]["evidence_id"] == "uncertain"


def test_rematch_supersedes_old_current_memberships_even_when_uncertain():
    run = run_state()
    row = material(run, "ev")
    run["memberships"] = [{"evidence_id": "ev", "dimension_id": "dim-1", "version": 1}]
    task = {"stage": "match", "payload": {}, "registry_revision": 0, "unit_id": "unit-match"}
    consume(run, {"packet": task}, {"issues": [], "result": {"decisions": [
        {"evidence_id": "ev", "action": "uncertain", "targets": [], "method_text": "", "reason": "需要复核"}]}})
    assert not run["memberships"][0]["active"]
    assert evidence_context.representatives(run, [dimension()])[0] == []
    assert flow.unresolved(run) == [row]


@pytest.mark.asyncio
async def test_changed_dimension_rechecks_every_original_case():
    run, repo = run_state(), MemoryRepository()
    raw = next(iter(run["cases"].values()))
    for i in range(20):
        other = {**copy.deepcopy(raw), "case_ref": "case_" + f"{i:024x}"}
        run["cases"][case_key(other)] = other
    run.update(stage="recheck", changed=["new"], dimensions={"old": dimension("old"), "new": dimension("new")})
    await flow.plan(None, None, repo, run)
    assert len(run["queue"]) == len(run["cases"])
    assert all(u["packet"]["payload"]["dimensions"][0]["dimension_id"] == "new"
               for k, u in repo.records.items() if k.startswith("unit-"))


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind", ["rubric", "evidence", "gap"])
async def test_delivery_cannot_be_complete_with_unresolved_business_items(monkeypatch, pending_kind):
    from team_miner.skill_miner.dialogue_dimensions.v2 import delivery

    run, repo = run_state(), MemoryRepository()
    raw = next(iter(run["cases"].values()))
    run["extracted"][case_key(raw)] = "unit-extract"
    run["dimensions"] = {"dim-1": dimension()}
    run["audits"] = [{"case_ref": raw["case_ref"], "case_version": raw["case_version"],
        "dimension_id": "dim-1", "version": 1, "signal": "support", "applicable": "yes", "sufficient": "yes"}]
    run["rubrics"]["dim-1"] = {"dimension_id": "dim-1", "version": 1,
        "rubric_status": "pending" if pending_kind == "rubric" else "provisional"}
    if pending_kind == "evidence":
        material(run, "uncertain")
        run["dispositions"]["uncertain"] = "uncertain"
    if pending_kind == "gap":
        run["gaps"] = [{**gap(run), "status": "needs_review"}]

    async def artifact(*args):
        pass

    async def update(engine, job, **fields):
        return fields

    monkeypatch.setattr(delivery, "artifact", artifact)
    monkeypatch.setattr(delivery, "update", update)
    result = await delivery.finish(None, {}, repo, run)
    assert result["delivery_status"] == "partial"
    assert result[f"pending_{pending_kind}_count"] == 1
    assert result["independent_calibration"] == "pending"


def test_conflicting_revision_packets_preserve_the_first_proposal_and_report_gap():
    run = run_state()
    run["dimensions"] = {"dim-1": dimension()}
    result = {"changes": [{"action": "update", "source_dimension_ids": ["dim-1"], "reason": "修正定义",
        "support_refs": [], "replacement_cards": [{"proposal_ref": "p", "card": dimension()["card"],
                                                    "member_refs": []}]}]}
    for uid in ["unit-1", "unit-2"]:
        task = {"stage": "revise", "payload": {}, "registry_revision": 0, "unit_id": uid}
        consume(run, {"packet": task}, {"issues": [], "result": result})
    assert len(run["proposals"]) == 1
    assert run["issues"][-1]["reason"] == "revision_conflict"


def test_demoted_dimension_triggers_rematch_instead_of_leaving_attached_orphans():
    run = run_state()
    run.update(stage="revise", dimensions={"dim-1": dimension()}, pending_demotions={"dim-1": "needs_review"})
    flow.advance(run)
    assert run["changed"] == ["dim-1"]
    assert run["dimensions"]["dim-1"]["status"] == "needs_review"


@pytest.mark.asyncio
async def test_global_gap_addition_rechecks_sources_and_is_resolved_by_final_registry(monkeypatch):
    from team_miner.skill_miner.dialogue_dimensions.input import canonical
    from team_miner.skill_miner.dialogue_dimensions.v2.validation import validate
    from tests.support.dialogue_dimensions_v2 import receipt

    run, repo = run_state(), MemoryRepository()
    row = material(run, "ev")
    original = dimension()
    run.update(stage="audit", dimensions={"dim-1": original}, dispositions={"ev": "attached"})
    run["memberships"] = [{"dimension_id": "dim-1", "version": 1, "evidence_id": "ev", "method_text": ""}]
    seen = []

    async def recall(engine, job, repo, state, rows, dimensions, limit):
        return {e["evidence_id"]: [d["dimension_id"] for d in dimensions] for e in rows}

    async def step(engine, job, repo, state, consumer):
        for uid in list(state["queue"]):
            unit = await repo.get(uid)
            if unit["status"] == "accepted":
                continue
            task = unit["packet"]
            p, stage = task["payload"], task["stage"]
            seen.append((state["stage"], stage))
            if stage == "audit":
                result = {"assessments": [{"dimension_id": d["dimension_id"], "version": d["version"],
                    "applicable": "yes", "sufficient": "yes", "signal": "support", "support_refs": ["s000001"],
                    "counter_refs": [], "reason": "完整原文支持"} for d in p["dimensions"]],
                    "uncovered": [{k: v for k, v in gap(state).items()
                                   if k in {"sentence_refs", "issue", "suggested_question"}}]
                    if state["stage"] == "audit" else []}
            elif stage == "register":
                result = {"comparisons": [{"dimension_id": d["dimension_id"], "version": d["version"],
                    "relation": "same_dimension" if p.get("gap_ref") and d["dimension_id"] != "dim-1"
                    else "different", "reason": "核对评价目标"} for d in p["dimensions"]]}
            elif stage == "revise":
                if p["audit_gaps"]:
                    result = {"changes": [{"action": "add", "source_dimension_ids": [], "reason": "全目录确认缺失",
                        "support_refs": [p["spans"][0]["span_ref"]], "replacement_cards": [{"proposal_ref": "new",
                        "card": {**original["card"], "name": "新增目标"},
                        "member_refs": [p["audit_gaps"][0]["member_ref"]]}]}]}
                else:
                    result = {"changes": [{"action": "keep", "source_dimension_ids": [d["dimension_id"]],
                        "reason": "保持", "support_refs": []} for d in p["dimensions"]]}
            elif stage == "match":
                result = {"decisions": [{"evidence_id": row["evidence_id"], "action": "attach",
                    "targets": [{"dimension_id": "dim-1", "version": 1, "relation": "same_dimension"}],
                    "method_text": "", "reason": "原维度仍适合"}]}
            elif stage == "rubric":
                d = p["dimension"]
                result = {"dimension_id": d["dimension_id"], "version": d["version"], "rubric_status": "pending",
                    "applicability": {"when": "存在机会", "na": "无机会", "ne": "不足"}, "anchors": [],
                    "method_rules": [], "calibration_notes": ["仍需校准"]}
            else:
                raise AssertionError(stage)
            consumer(state, unit, validate(canonical(receipt(task, result)), task))
            unit["status"] = "accepted"
            await repo.save(state, **{uid: unit})
        state["queue"] = []

    monkeypatch.setattr(flow, "recall", recall)
    monkeypatch.setattr(flow.executor, "step", step)
    for _ in range(100):
        if run["stage"] == "assemble":
            break
        if run["stage"] == "register":
            await flow.registration(None, None, repo, run)
        elif run["queue"]:
            await step(None, None, repo, run, consume)
        elif run["planned"]:
            flow.advance(run)
        else:
            await flow.plan(None, None, repo, run)
    assert run["stage"] == "assemble"
    assert len(run["dimensions"]) == 2
    assert len(run["audits"]) == 2
    assert run["gaps"][0]["status"] == "covered"
    assert ("gap_review_final", "register") in seen
    assert ("recheck", "audit") in seen


@pytest.mark.asyncio
async def test_candidate_pages_preserve_first_match_after_later_unmatched_page(monkeypatch):
    run, repo = run_state(), MemoryRepository()
    row = material(run, "ev")
    run["stage"] = "expanded_match"
    dims = [dimension("first"), dimension("second")]
    run["dimensions"] = {d["dimension_id"]: d for d in dims}
    original = flow.packet

    def one_dimension(stage, payload, *args):
        if len(payload["dimensions"]) > 1:
            raise ValueError("capacity_pending")
        return original(stage, payload, *args)

    monkeypatch.setattr(flow, "packet", one_dimension)
    await flow.enqueue(repo, run, "match", {"evidence": [row], "dimensions": dims,
        "allowed_candidates": {"ev": ["first", "second"]}}, "evidence")
    assert len(run["queue"]) == 2
    for uid in run["queue"]:
        unit = repo.records[uid]
        target = unit["packet"]["payload"]["dimensions"][0]["dimension_id"]
        decision = {"evidence_id": "ev", "action": "attach" if target == "first" else "unmatched",
            "targets": [{"dimension_id": "first", "version": 1, "relation": "same_dimension"}]
            if target == "first" else [], "method_text": "", "reason": "当前页的判断"}
        consume(run, unit, {"issues": [], "result": {"decisions": [decision]}})
        if target == "first":
            assert run["dispositions"]["ev"] == "deferred"
            assert not run["memberships"]
    assert run["dispositions"]["ev"] == "attached"
    assert [m["dimension_id"] for m in run["memberships"]] == ["first"]
    assert next(iter(run["match_scopes"].values()))["status"] == "complete"


def test_compact_source_versions_do_not_conflate_two_versions_of_same_case():
    from team_miner.skill_miner.dialogue_dimensions.input import normalize_case

    run = run_state()
    first = material(run, "first")
    raw = next(iter(run["cases"].values()))
    other = normalize_case({k: v for k, v in {**raw, "dialogue_text": "HWP：另一个目标？\nC：五年后。"}.items()
                            if k not in {"case_version", "sentence_index"}})
    run["cases"][case_key(other)] = other
    second = {**first, "evidence_id": "second", "member_ref": "second", "case_version": other["case_version"]}
    run["evidence"]["second"] = second
    payload = evidence_context.ground(run, {"evidence": [first, second]})
    assert raw["case_ref"] not in payload["source_versions"]
    assert {s["case_version"] for s in payload["spans"]} == {raw["case_version"], other["case_version"]}
    assert set(payload["evidence"][0]["source_span_refs"]).isdisjoint(payload["evidence"][1]["source_span_refs"])


def test_proposed_evidence_cannot_be_hidden_as_routine_when_registration_defers():
    from team_miner.skill_miner.dialogue_dimensions.v2.registry import register

    run = run_state()
    material(run, "mixed", status="mixed")
    run["dispositions"]["mixed"] = "unresolved"
    proposal = {"proposal_ref": "p", "card": dimension()["card"], "member_refs": ["mixed"], "rationale": "反证"}
    unit = {"packet": {"stage": "propose", "unit_id": "p-unit", "registry_revision": 0, "payload": {}}}
    consume(run, unit, {"issues": [], "result": {"proposals": [proposal], "dispositions": [
        {"member_ref": "mixed", "action": "routine", "target_dimension_id": None, "reason": "已纳入候选"}]}})
    assert run["dispositions"]["mixed"] == "unresolved"
    assert run["issues"][-1]["reason"] == "proposal_disposition_conflict"
    run["dimensions"] = {"dim-1": dimension()}
    # Includes legacy receipts accepted before overlap protection was deployed.
    run["dispositions"]["mixed"] = "routine"
    register(run, run["proposals"][0], [])
    assert run["proposals"][0]["status"] == "deferred"
    assert run["dispositions"]["mixed"] == "deferred"
    assert [row["evidence_id"] for row in flow.unresolved(run)] == ["mixed"]
