"""Length-aware batching preserves frozen inputs and single-unit progress."""

from types import SimpleNamespace

import pytest

from team_miner.skill_miner.default_compiler import SHA256, SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical
from team_miner.skill_miner.dialogue_evidence import executor
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan
from team_miner.skill_miner.dialogue_evidence.timing import commit_timings
from tests.support.dialogue_dimensions_v2 import MemoryRepository
from tests.support.dialogue_evidence import CompileMemoryHost, case


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_pending", [None, "unaccepted", "accepted"])
@pytest.mark.parametrize("vectors_only", [None, 0, 1])
@pytest.mark.parametrize("chars,soft,expected", [
    (100, 8000, 3), (3000, 8000, 2), (8200, 8000, 1), (3000, 0, 3),
])
async def test_batches_bound_combined_input_without_truncation(
        monkeypatch, chars, soft, expected, vectors_only, legacy_pending):
    host, repo = CompileMemoryHost(), MemoryRepository()
    options = {**DEFAULTS, "compile_input_soft_chars": soft}
    if vectors_only is not None:
        options["output_vectors_only"] = vectors_only  # Legacy snapshot must not change the OV contract.
    units = [plan(case("HWP：" + "问" * chars + "\nC：可以。"), options, i)[0] for i in range(1, 5)]
    originals = {u["packet"]["unit_id"]: canonical(u["packet"]) for u in units}
    run = {"queue": list(originals), "active": None, "options": options, "skill_hash": SHA256,
           "metrics": {"compiles": 0}}
    await repo.save(run, **{u["packet"]["unit_id"]: u for u in units})
    monkeypatch.setattr(executor, "write", host.write)
    monkeypatch.setattr(executor, "update", host.update)
    engine = SimpleNamespace(tenant="synthetic", _round_client=lambda job: None,
        _round_provision_compiler=host.noop, _knowledge_request=host.request,
        admission=SimpleNamespace(acquire=host.noop, release=host.noop, mark=host.noop))
    job = {"job_id": "batch", "account": "synthetic", "compiler_body_snapshot": SKILL_BODY,
           "runtime_timeout_seconds": 3600}
    await executor.step(engine, job, repo, run, None)
    active = (await repo.get("run"))["active"]
    assert "output_processing_mode" not in active["request"]
    if legacy_pending:
        active.pop("task_id")
        active["request"]["output_processing_mode"] = "vectors_only"
        old_key = active["key"]
        if legacy_pending == "unaccepted":
            host.submissions.clear()
        run["active"] = active
        await repo.save(run)
        await executor.step(engine, job, repo, run, None)
        if legacy_pending == "unaccepted":
            assert active["key"] != old_key
            assert "output_processing_mode" not in active["request"]
            await executor.step(engine, job, repo, run, None)
        else:
            assert active["key"] == old_key
            assert len(host.tasks) == 1
        assert active["task_id"]
    assert len(active["units"]) == expected
    assert active["input_chars"] == sum(len(originals[k]) for k in active["units"])
    for key in active["units"]:
        assert host.files[active["request"]["from"][0] + "/" + key + ".md"] == originals[key]
    assert run["queue"] == list(originals)
    assert (await repo.get("unit-000004"))["status"] == "planned"


def test_commit_observations_reject_unknown_and_nonfinite_remote_fields():
    observed = commit_timings({"meta": {"compile_commit_timings": {
        "snapshot": {"seconds": 3.5, "calls": 2, "last_outcome": "completed", "private": "discard"},
        "diff": {"seconds": float("nan"), "calls": 1},
        "versions": {"seconds": -1, "calls": 1},
        "unknown": {"seconds": 4, "calls": 1},
    }}})
    assert observed == {"snapshot": {"seconds": 3.5, "calls": 2, "last_outcome": "completed"}}
    assert commit_timings({}) == {}
