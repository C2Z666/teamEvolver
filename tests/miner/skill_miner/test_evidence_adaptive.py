"""Conservative tuning boundaries, restart behavior and partial salvage."""

import copy
import json

import pytest

from team_miner.skill_miner.default_compiler import SHA256
from team_miner.skill_miner.dialogue_dimensions.input import canonical, case_key, sha
from team_miner.skill_miner.dialogue_evidence import adaptive, recovery
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan
from team_miner.skill_miner.dialogue_evidence.delivery import build
from team_miner.skill_miner.dialogue_evidence.orchestration import consume
from team_miner.skill_miner.dialogue_evidence.validation import validate
from tests.support.dialogue_dimensions_v2 import MemoryRepository
from tests.support.dialogue_evidence import case, receipt


def sample():
    run = {"options": dict(DEFAULTS), "metrics": {"accepted": 0, "retries": 0},
           "evidence": {}, "issues": [], "dataset_hash": "frozen", "skill_hash": SHA256}
    return run, {"job_id": "job", "runtime_timeout_seconds": 3600}


def completed(run, job, number, *, tokens=120000, seconds=400, unhealthy=False):
    active = {"task_id": f"compile-{number}", "units": list(range(adaptive.batch_limit(run, job)))}
    task = {"status": "completed", "meta": {"pipeline": {"tokens": {"input": tokens, "output": 1000,
                                                                  "cached": tokens}}}}
    adaptive.observe(run, job, task=task, active=active, seconds=seconds, unhealthy=unhealthy)


def test_slow_growth_has_hard_ceiling_and_survives_restart():
    run, job = sample()
    completed(run, job, 1)
    completed(run, job, 2)
    assert adaptive.batch_limit(run, job) == 4
    run = json.loads(json.dumps(run))
    completed(run, job, 3)
    assert adaptive.batch_limit(run, job) == 5
    for i in range(4, 20):
        completed(run, job, i)
    assert adaptive.batch_limit(run, job) == 6
    before = copy.deepcopy(run)
    completed(run, job, 19)
    assert run == before


@pytest.mark.parametrize("pressure", ["tokens", "latency", "failure"])
def test_pressure_shrinks_batch_and_never_shortens_timeout(pressure):
    run, job = sample()
    kwargs = {"tokens": 400000} if pressure == "tokens" else (
        {"seconds": 2500} if pressure == "latency" else {"unhealthy": True})
    completed(run, job, 1, **kwargs)
    assert adaptive.batch_limit(run, job) == 2
    assert 3600 <= adaptive.timeout_seconds(run, job) <= 7200
    previous = adaptive.timeout_seconds(run, job)
    completed(run, job, 2)
    assert adaptive.timeout_seconds(run, job) == previous


def test_missing_usage_disables_growth_and_legacy_runs_stay_fixed():
    run, job = sample()
    for i in range(6):
        adaptive.observe(run, job, task={"status": "completed"},
                         active={"task_id": str(i), "units": [1, 2, 3, 4]}, seconds=300)
    assert adaptive.batch_limit(run, job) == 4
    run["options"].pop("adaptive_batching")
    completed(run, job, 7, tokens=900000)
    assert adaptive.batch_limit(run, job) == 4


def test_projected_consumption_blocks_growth_even_when_each_batch_is_under_limit():
    run, job = sample()
    for i in range(8):
        completed(run, job, i, tokens=280000)
    assert adaptive.batch_limit(run, job) == 4
    assert all(v["reason"] == "hold" for v in run["adaptive"]["decisions"])


@pytest.mark.asyncio
async def test_field_repair_checkpoint_retains_partial_and_clears_recovered_error():
    run, job = sample()
    source = case()
    unit = plan(source, DEFAULTS)[0]
    packet = unit["packet"]
    run.update(cases={case_key(source): source}, units=[packet["unit_id"]], budget_profile={},
               execution_hash="hash")
    invalid = receipt(packet)
    invalid["result"]["evidence"][0]["trigger"] = {
        "text": "事后客户回应", "sentence_refs": ["s000002"], "metadata_refs": []}
    text = canonical(invalid)
    value = validate(text, packet)
    assert value["quarantined"][0]["detail"] == "trigger_after_action"
    repo = MemoryRepository()
    await repo.save(run, **{packet["unit_id"]: unit})
    await recovery.handle_receipt(repo, run, unit, value, text, {"task_id": "first"},
                                  "viking://resources/first/result.json", consume, job["job_id"])
    assert not run["evidence"]
    unit = await repo.get(packet["unit_id"])
    assert unit["fallback"]["receipt_hash"] == sha(text)
    assert unit["status"] == "retry_wait"
    # A process restart then an unusable repair still preserves the accepted part of the original receipt.
    run = await repo.get("run")
    await recovery.accept(repo, run, unit, unit["fallback"], consume, job["job_id"])
    assert len(run["evidence"]) == 1 and unit["error"] == ""
    assert unit["last_attempt_error"]
    assert unit["receipt_uri"] == "viking://resources/first/result.json"
    assert build(job, run, [unit])[1]["delivery_status"] == "partial"


@pytest.mark.asyncio
async def test_valid_repair_replaces_fallback_without_leaking_old_isolation():
    run, job = sample()
    source = case()
    unit = plan(source, DEFAULTS)[0]
    packet = unit["packet"]
    bad = receipt(packet)
    bad["result"]["evidence"][0]["trigger"] = {
        "text": "晚于动作", "sentence_refs": ["s000002"], "metadata_refs": []}
    repo = MemoryRepository()
    await recovery.handle_receipt(repo, run, unit, validate(canonical(bad), packet), canonical(bad),
                                  {"task_id": "bad"}, "viking://resources/bad/result.json", consume, job["job_id"])
    good = canonical(receipt(packet))
    await recovery.handle_receipt(repo, run, unit, validate(good, packet), good,
                                  {"task_id": "good"}, "viking://resources/good/result.json", consume, job["job_id"])
    assert unit["status"] == "accepted" and unit["error"] == ""
    assert unit["receipt_hash"] == sha(good)
    assert "fallback" not in unit and not run["issues"]
    assert run["metrics"]["accepted"] == len(run["evidence"]) == 1
