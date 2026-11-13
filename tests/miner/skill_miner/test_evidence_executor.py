from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.default_compiler import SHA256, SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical, case_key
from team_miner.skill_miner.dialogue_evidence import executor
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan
from team_miner.skill_miner.dialogue_evidence.orchestration import consume, split_unit
from tests.support.dialogue_dimensions_v2 import MemoryRepository
from tests.support.dialogue_evidence import case, receipt


@pytest.mark.asyncio
async def test_lost_acceptance_recovers_same_key_and_consumes_only_once(monkeypatch):
    source = case()
    unit = plan(source, DEFAULTS)[0]
    packet = unit["packet"]
    run = {"queue": [packet["unit_id"]], "active": None, "skill_hash": SHA256,
           "dataset_hash": "frozen", "options": DEFAULTS, "issues": [], "evidence": {},
           "metrics": {"compiles": 0, "accepted": 0, "retries": 0}}
    repo = MemoryRepository()
    await repo.save(run, **{packet["unit_id"]: unit})
    store, submissions, posts = {}, {}, []
    job = {"job_id": "job1", "account": "test", "compiler_body_snapshot": SKILL_BODY,
           "runtime_timeout_seconds": 3600}

    async def write(engine, job, uri, text):
        store[uri] = text

    async def read(engine, job, uri):
        if uri not in store:
            raise HTTPException(404, "absent")
        return store[uri]

    async def update(engine, job, **fields):
        job.update(fields)
        return job

    async def noop(*args, **kwargs):
        return True

    async def request(account, method, path, **kwargs):
        if path == "/api/v1/fs/ls":
            return [{"uri": key} for key in store if key.startswith(kwargs["params"]["uri"]+"/")]
        if path.startswith("/api/v1/compile/submissions/"):
            key = path.rsplit("/", 1)[-1]
            if key not in submissions:
                raise HTTPException(404, "absent")
            return {"task_id": submissions[key]}
        if path == "/api/v1/compile":
            key = kwargs["extra_headers"]["Idempotency-Key"]
            posts.append(key)
            submissions[key] = "remote"
            store[kwargs["json"]["to"]+"/"+packet["output_path"]] = canonical(receipt(packet))
            raise TimeoutError("accepted, acknowledgement lost")
        return {"task_id": "remote", "status": "completed"}

    engine = SimpleNamespace(tenant="test", _round_client=lambda job: None, _round_provision_compiler=noop,
                             _knowledge_request=request,
                             admission=SimpleNamespace(acquire=noop, release=noop, mark=noop))
    monkeypatch.setattr(executor, "write", write)
    monkeypatch.setattr(executor, "read", read)
    monkeypatch.setattr(executor, "update", update)
    with pytest.raises(TimeoutError):
        await executor.step(engine, job, repo, run, consume)
    run = await repo.get("run")
    await executor.step(engine, job, repo, run, consume)
    await executor.step(engine, job, repo, run, consume)
    await executor.step(engine, job, repo, run, consume)
    assert len(posts) == len(run["evidence"]) == run["metrics"]["accepted"] == 1
    assert (await repo.get(packet["unit_id"]))["remote_task_id"] == "remote"


@pytest.mark.asyncio
async def test_capacity_split_is_atomic_and_has_finite_depth():
    source = case()
    unit = plan(source, DEFAULTS)[0]
    identifier = unit["packet"]["unit_id"]
    run = {"cases": {case_key(source): source}, "options": DEFAULTS, "units": [identifier],
           "queue": [identifier], "unit_count": 1, "metrics": {"splits": 0}}
    repo = MemoryRepository()
    await repo.save(run, **{identifier: unit})
    await split_unit(repo, run, unit)
    assert (await repo.get(identifier))["status"] == "superseded"
    assert len(run["units"]) > 1
    child = await repo.get(run["units"][1])
    child["depth"] = DEFAULTS["capacity_split_depth"]
    await split_unit(repo, run, child)
    assert (await repo.get(child["packet"]["unit_id"]))["status"] == "failed"
