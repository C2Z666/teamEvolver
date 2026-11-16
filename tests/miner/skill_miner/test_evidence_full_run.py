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
@pytest.mark.parametrize("patch_valid", [True, False])
async def test_171_cases_finish_after_restart_malformed_output_and_field_repair(monkeypatch, patch_valid):
    cases = {}
    for i in range(171):
        source = normalize_case({"case_ref": "case_" + f"{i:024x}",
            "dialogue_text": f"HWP：先确认交流条件{i}。\nC：可以。\nHWP：我们约十分钟。"})
        cases[case_key(source)] = source
    repo, host = MemoryRepository(), CompileMemoryHost()
    host.lose_resolved_response = patch_valid
    host.invalid_patch = not patch_valid

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
    assert job["status"] == ("succeeded" if patch_valid else "interrupted")
    assert restarts == (4 if patch_valid else 3)
    assert job["evidence_accepted_units"] == 171
    assert job["processed_case_count"] == (171 if patch_valid else 170)
    run = await repo.get("run")
    assert len(run["evidence"]) == run["metrics"]["accepted"] == 171
    assert run["metrics"]["retries"] == 2 and run["metrics"]["field_repairs"] == 1
    assert all(count == (2 if unit in {"unit-000013", "unit-000027"} else 1)
               for unit, count in host.calls.items())
    assert (await repo.get("unit-000013"))["error"] == ""
    assert bool((await repo.get("unit-000027"))["receipt"]["quarantined"]) == (not patch_valid)
    assert 1 <= run["adaptive"]["batch_limit"] <= 4
    assert any(d["reason"] == "grow_one" for d in run["adaptive"]["decisions"])
    assert not any(d["reason"] == "backoff" for d in run["adaptive"]["decisions"])
    assert host.submission_modes[-1] == "field_patch"
    if patch_valid:
        assert run["metrics"]["field_patch_successes"] == 1
        assert (await repo.get("unit-000027"))["repair_provenance"]["merged_uri"].endswith("unit-000027.json")
    else:
        assert run["metrics"]["field_patch_fallbacks"] == 1
        assert not (await repo.get("unit-000027")).get("repair_provenance")
    import json
    manifest = json.loads(host.artifacts["manifest.json"])
    assert manifest["delivery_status"] == ("complete" if patch_valid else "partial")
    assert manifest["case_count"] == 171
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
    assert run["options"]["output_vectors_only"] == 0
    assert not run["options"]["compile_input_soft_chars"]
    assert not run["options"]["adaptive_batching"]
    assert not run["options"]["field_repair_attempts_per_unit"]
    assert run["options"]["repair_attempts_per_unit"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,count,legacy,expected", [
    (259200, 1, False, 259200),
    (259200, 171, False, 259200),
    (86400, 171, False, 86400),
    (86400, 171, True, 172800),
])
async def test_whole_job_timeout_uses_frozen_budget_without_case_multiplier(
        monkeypatch, seconds, count, legacy, expected):
    from team_miner.skill_miner.dialogue_evidence import adaptive
    cases = {}
    for i in range(count):
        source = normalize_case({"case_ref": "case_" + f"{i:024x}", "dialogue_text": "HWP：方便约十分钟吗？"})
        cases[case_key(source)] = source

    async def load(*args):
        return cases, [], "frozen-dataset"

    monkeypatch.setattr(orchestration, "load_cases", load)
    options = dict(DEFAULTS)
    if legacy:
        options.pop("runtime_timeout_is_job_deadline")
        options.pop("compile_initial_timeout_seconds")
        options["job_deadline_seconds"] = 172800
    job = {"compiler_body_snapshot": SKILL_BODY, "evidence_options": options,
           "runtime_timeout_seconds": seconds}
    repo = MemoryRepository()
    run = await orchestration.prepare(None, job, repo)
    assert run["deadline_seconds"] == expected
    assert adaptive.timeout_seconds(run, job) == (seconds if legacy else 3600)
    # Restoring the checkpoint must preserve the accepted policy.
    restored = await repo.get("run")
    assert restored["deadline_seconds"] == expected
    assert adaptive.timeout_seconds(restored, job) == (seconds if legacy else 3600)
