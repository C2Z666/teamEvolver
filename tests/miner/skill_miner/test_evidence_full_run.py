"""Whole-run recovery and delivery using synthetic sources, without model calls."""

from types import SimpleNamespace

import pytest

from team_miner.skill_miner.default_compiler import SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import case_key, normalize_case
from team_miner.skill_miner.dialogue_evidence import delivery, executor, orchestration
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS
from tests.support.dialogue_dimensions_v2 import MemoryRepository
from tests.support.dialogue_evidence import CompileMemoryHost


@pytest.mark.asyncio
async def test_171_cases_finish_after_restart_malformed_output_and_field_repair(monkeypatch):
    cases = {}
    for i in range(171):
        source = normalize_case({"case_ref": "case_" + f"{i:024x}",
            "dialogue_text": f"HWP：先确认交流条件{i}。\nC：可以。\nHWP：我们约十分钟。"})
        cases[case_key(source)] = source
    repo, host = MemoryRepository(), CompileMemoryHost()

    async def load(*args):
        return cases, [], "synthetic-dataset"

    monkeypatch.setattr(orchestration, "Repository", lambda *args: repo)
    monkeypatch.setattr(orchestration, "load_cases", load)
    monkeypatch.setattr(orchestration, "update", host.update)
    monkeypatch.setattr(executor, "update", host.update)
    monkeypatch.setattr(executor, "write", host.write)
    monkeypatch.setattr(executor, "read", host.read)
    monkeypatch.setattr(delivery, "update", host.update)
    monkeypatch.setattr(delivery, "artifact", host.artifact)
    engine = SimpleNamespace(tenant="synthetic", _round_client=lambda job: None,
        _round_provision_compiler=host.noop, _knowledge_request=host.request,
        admission=SimpleNamespace(acquire=host.noop, release=host.noop, mark=host.noop))
    job = {"job_id": "full-run", "account": "synthetic", "compiler_body_snapshot": SKILL_BODY,
           "evidence_options": dict(DEFAULTS), "runtime_timeout_seconds": 3600}
    restarts = 0
    for _ in range(1000):
        try:
            await orchestration.tick(engine, job)
        except ConnectionError:
            # Simulate reconstructing the worker from PG after a lost read response.
            assert (await repo.get("unit-000001"))["status"] == "accepted"
            restarts += 1
        if job.get("dialogue_stage") == "finished":
            break
    assert job["status"] == "succeeded" and restarts == 3
    assert job["processed_case_count"] == job["evidence_accepted_units"] == 171
    run = await repo.get("run")
    assert len(run["evidence"]) == run["metrics"]["accepted"] == 171
    assert run["metrics"]["retries"] == 2 and run["metrics"]["field_repairs"] == 1
    assert all(count == (2 if unit in {"unit-000013", "unit-000027"} else 1)
               for unit, count in host.calls.items())
    assert (await repo.get("unit-000013"))["error"] == ""
    assert (await repo.get("unit-000027"))["receipt"]["quarantined"] == []
    assert 1 <= run["adaptive"]["batch_limit"] <= 6
    assert any(d["reason"] == "grow_one" for d in run["adaptive"]["decisions"])
    assert any(d["reason"] == "backoff" for d in run["adaptive"]["decisions"])
    import json
    manifest = json.loads(host.artifacts["manifest.json"])
    assert manifest["delivery_status"] == "complete" and manifest["case_count"] == 171
    assert len(host.artifacts["cases-0001.jsonl"].splitlines()) == 171


@pytest.mark.asyncio
async def test_old_accepted_options_do_not_enable_new_policy(monkeypatch):
    from tests.support.dialogue_evidence import case
    source = case()
    repo = MemoryRepository()

    async def load(*args):
        return {case_key(source): source}, [], "frozen"

    monkeypatch.setattr(orchestration, "load_cases", load)
    old_options = {k: v for k, v in DEFAULTS.items() if k in {
        "packets_per_compile", "packet_input_chars", "packet_input_bytes", "compile_input_chars",
        "max_inflight_compiles", "repair_attempts_per_unit", "transport_retry_attempts",
        "transport_retry_delay_seconds", "capacity_split_depth"}}
    old_options["repair_attempts_per_unit"] = 1
    job = {"compiler_body_snapshot": SKILL_BODY, "evidence_options": old_options,
           "runtime_timeout_seconds": 3600}
    run = await orchestration.prepare(None, job, repo)
    assert not run["options"]["adaptive_batching"]
    assert not run["options"]["field_repair_attempts_per_unit"]
    assert run["options"]["repair_attempts_per_unit"] == 1
