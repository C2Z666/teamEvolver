import copy
from types import SimpleNamespace

import pytest

from team_miner.skill_miner.dialogue_dimensions.input import canonical
from team_miner.skill_miner.dialogue_dimensions.v2 import orchestration as flow
from team_miner.skill_miner.dialogue_dimensions.v2.registry import consume, current
from team_miner.skill_miner.dialogue_dimensions.v2.validation import validate
from tests.support.dialogue_dimensions_v2 import MemoryRepository, card, observation, receipt, run_state


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_one_job_reaches_delivery_without_merge_or_second_extraction(monkeypatch, empty):
    repo, run = MemoryRepository(), run_state()

    async def knowledge(*args, **kwargs):
        return {"model": "model-1", "vectors": [[1.0, 0.0] for _ in kwargs["json"]["texts"]]}

    engine = SimpleNamespace(_knowledge_request=knowledge)
    job = {"account": "test", "input_dir": "viking://resources/test"}
    stages, consumed = [], set()

    async def model_step(engine, job, repo, state, consumer):
        for identifier in list(state["queue"]):
            unit = await repo.get(identifier)
            if unit["status"] == "accepted":
                continue
            assert identifier not in consumed
            task, result = unit["packet"], None
            stage, payload = task["stage"], task["payload"]
            stages.append(stage)
            if stage == "extract":
                result = {"observations": [] if empty else [observation()], "empty_reason": "普通问候" if empty else ""}
            elif stage == "propose":
                result = {"proposals": [{"proposal_ref": "p1", "card": card(),
                           "member_refs": [row["member_ref"] for row in payload["evidence"]], "rationale": "同一目标"}],
                          "dispositions": []}
            elif stage == "match":
                result = {"decisions": [{"evidence_id": row["evidence_id"], "action": "uncertain", "targets": [],
                           "method_text": "", "reason": "不足"} for row in payload["evidence"]]}
            elif stage == "audit":
                result = {"assessments": [{"dimension_id": d["dimension_id"], "version": d["version"],
                           "applicable": "yes", "sufficient": "yes", "signal": "support", "support_refs": ["s000001"],
                           "counter_refs": [], "reason": "目标问题"} for d in payload["dimensions"]], "uncovered": []}
            elif stage == "revise":
                result = {"changes": [{"action": "keep", "source_dimension_ids": [d["dimension_id"]],
                           "reason": "边界稳定", "support_refs": []} for d in payload["dimensions"]]}
            elif stage == "rubric":
                d = payload["dimension"]
                result = {"dimension_id": d["dimension_id"], "version": d["version"], "rubric_status": "pending",
                           "applicability": {"when": "有目标", "na": "无机会", "ne": "片段不足"}, "anchors": [],
                           "method_rules": [], "calibration_notes": ["需要独立校准"]}
            else:
                raise AssertionError(stage)
            accepted = validate(canonical(receipt(task, result)), task)
            consumer(state, unit, accepted)
            unit["status"] = "accepted"
            consumed.add(identifier)
            await repo.save(state, **{identifier: unit})
        state["queue"] = []
        await repo.save(state)

    monkeypatch.setattr(flow.executor, "step", model_step)
    for _ in range(100):
        if run["stage"] == "assemble":
            break
        if run["stage"] == "register":
            await flow.registration(engine, job, repo, run)
        elif run["queue"]:
            await model_step(engine, job, repo, run, consume)
        elif not run["planned"]:
            await flow.plan(engine, job, repo, run)
        else:
            flow.advance(run)
    assert run["stage"] == "assemble"
    assert stages.count("extract") == 1
    assert "merge" not in stages
    assert len(run["extracted"]) == 1
    assert len(current(run)) == (0 if empty else 1)
    if not empty:
        assert stages.count("rubric") == 1
        assert flow.counts(run, current(run)[0])["covered"] == 1
        assert set(run["dispositions"].values()) == {"attached"}


@pytest.mark.asyncio
async def test_restarted_planner_does_not_duplicate_frozen_units():
    repo, run = MemoryRepository(), run_state()
    await flow.plan(None, None, repo, run)
    before = copy.deepcopy(repo.records)
    run["planned"] = False
    await flow.plan(None, None, repo, run)
    assert run["unit_count"] == 1
    assert run["queue"] == ["unit-000001"]
    assert repo.records["unit-000001"] == before["unit-000001"]


def test_changed_semantics_excludes_previous_audit_from_current_statistics():
    run = run_state()
    dimension = {"dimension_id": "dim-1", "version": 2}
    case = next(iter(run["cases"].values()))
    run["audits"] = [{"dimension_id": "dim-1", "version": 1, "case_ref": case["case_ref"],
                      "case_version": case["case_version"], "signal": "na", "applicable": "no", "sufficient": "yes"}]
    assert flow.counts(run, dimension)["covered"] == 0
    assert flow.counts(run, dimension)["planned"] == 1


@pytest.mark.asyncio
async def test_delivery_preserves_span_identity_and_reports_partial(monkeypatch):
    import json

    from team_miner.skill_miner.dialogue_dimensions.v2 import delivery

    repo, run = MemoryRepository(), run_state()
    source = {"span_ref": "span-one", "case_ref": "case-one", "case_version": 1,
              "sentence_ids": ["s000001"], "text": "原文"}
    run["source_spans"] = {"span-one": source}
    run.update(dataset_hash="dataset", skill_hash="skill")
    outputs = {}

    async def artifact(engine, job, path, text):
        outputs[path] = text

    async def update(engine, job, **fields):
        return fields

    monkeypatch.setattr(delivery, "artifact", artifact)
    monkeypatch.setattr(delivery, "update", update)
    result = await delivery.finish(None, {}, repo, run)
    assert result["delivery_status"] == "partial"
    assert result["pending_case_count"] == 1
    assert json.loads(outputs["spans/part-000001.jsonl"]) == source
    metrics = json.loads(outputs["run_metrics.json"])
    assert metrics["input_cases"] == 1
    assert metrics["raw_dialogue_chars"] > 0
    assert metrics["phase_wall_seconds"] == "unavailable"


def test_legacy_sentence_count_does_not_truncate_behavior_or_counter_context():
    run = run_state()
    run["options"]["representative_spans"] = 1
    raw = next(iter(run["cases"].values()))
    identity = {k: raw[k] for k in ("case_ref", "case_version")}
    evidence = {**observation(), **identity}
    audit = {**identity, "support_refs": ["s000001"], "counter_refs": ["s000002"]}
    result = flow.spans(run, [evidence], [audit])
    assert {s["sentence_ids"][0] for s in result} == {"s000001", "s000002", "s000003"}
    assert any(s["speaker"] == "HWP" for s in result)


@pytest.mark.asyncio
async def test_rubric_excludes_superseded_memberships_audits_and_methods():
    run, repo = run_state(), MemoryRepository()
    raw = next(iter(run["cases"].values()))
    identity = {k: raw[k] for k in ("case_ref", "case_version")}
    dimension = {"dimension_id": "dim-1", "version": 2, "status": "active", "card": card()}
    run.update(stage="rubric", dimensions={"dim-1": dimension})
    for identifier, version in (("old", 1), ("new", 2)):
        run["evidence"][identifier] = {**observation(), **identity, "evidence_id": identifier,
            "member_ref": identifier, "trigger_refs": [], "action_refs": ["s000001"], "response_refs": []}
        run["memberships"].append({"evidence_id": identifier, "dimension_id": "dim-1", "version": version,
                                   "method_text": identifier + " method"})
    run["audits"] = [{**identity, "dimension_id": "dim-1", "version": 1, "signal": "counter",
                      "support_refs": [], "counter_refs": ["s000002"]}]
    evidence, sources = flow.representatives(run, [dimension])
    assert [row["evidence_id"] for row in evidence] == ["new"]
    assert all(row["sentence_ids"] == ["s000001"] for row in sources)
    await flow.plan(None, None, repo, run)
    unit = await repo.get(run["queue"][0])
    assert unit["packet"]["payload"]["methods"] == ["new method"]


@pytest.mark.asyncio
async def test_split_revision_packets_keep_only_their_dimensions_evidence_and_statistics(monkeypatch):
    run, repo = run_state(), MemoryRepository()
    raw = next(iter(run["cases"].values()))
    identity = {k: raw[k] for k in ("case_ref", "case_version")}
    run["stage"] = "revise"
    for identifier in ("dim-a", "dim-b"):
        run["dimensions"][identifier] = {"dimension_id": identifier, "version": 1, "status": "active", "card": card()}
        run["evidence"][identifier] = {**observation(), **identity, "evidence_id": identifier, "member_ref": identifier}
        run["memberships"].append({"evidence_id": identifier, "dimension_id": identifier, "version": 1})
    original_packet = flow.packet

    def single_dimension_packet(stage, payload, revision, ordinal, options):
        if len(payload["dimensions"]) > 1:
            raise ValueError("capacity_pending")
        return original_packet(stage, payload, revision, ordinal, options)

    monkeypatch.setattr(flow, "packet", single_dimension_packet)
    await flow.plan(None, None, repo, run)
    assert len(run["queue"]) == 2
    for identifier in run["queue"]:
        unit = await repo.get(identifier)
        payload = unit["packet"]["payload"]
        dimension_id = payload["dimensions"][0]["dimension_id"]
        assert [row["evidence_id"] for row in payload["evidence"]] == [dimension_id]
        assert set(payload["metrics"]) == {dimension_id}


@pytest.mark.asyncio
async def test_revision_capacity_sampling_preserves_whole_spans_and_records_selection(monkeypatch):
    run, repo = run_state(), MemoryRepository()
    raw = next(iter(run["cases"].values()))
    identity = {k: raw[k] for k in ("case_ref", "case_version")}
    run["stage"] = "revise"
    dimension = {"dimension_id": "dim-1", "version": 1, "status": "active", "card": card()}
    run["dimensions"] = {"dim-1": dimension}
    for identifier, value in (("positive", "support_candidate"), ("mixed", "mixed")):
        run["evidence"][identifier] = {**observation(), **identity, "evidence_id": identifier,
                                        "member_ref": identifier, "value_status": value}
        run["memberships"].append({"evidence_id": identifier, "dimension_id": "dim-1", "version": 1})
    original_spans = flow.representatives(run, [dimension])[1]
    original_packet = flow.packet

    def one_observation_packet(stage, payload, revision, ordinal, options):
        if len(payload["evidence"]) > 1:
            raise ValueError("capacity_pending")
        return original_packet(stage, payload, revision, ordinal, options)

    monkeypatch.setattr(flow, "packet", one_observation_packet)
    await flow.plan(None, None, repo, run)
    assert len(run["queue"]) == 1
    unit = await repo.get(run["queue"][0])
    assert [row["evidence_id"] for row in unit["packet"]["payload"]["evidence"]] == ["mixed"]
    assert unit["packet"]["payload"]["spans"] == original_spans
    assert len(run["evidence"]) == 2
    selection = run["representative_selections"][0]
    assert selection["summary"]["evidence"] == {"available": 2, "selected": 1, "omitted": 1}
    assert selection["omitted"][0]["row"]["evidence_id"] == "positive"
