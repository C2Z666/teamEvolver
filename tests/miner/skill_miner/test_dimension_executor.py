"""Lost acknowledgements and failed children use durable, per-unit recovery."""

from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.default_compiler import SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical
from team_miner.skill_miner.dialogue_dimensions.v2 import executor
from team_miner.skill_miner.dialogue_dimensions.v2.input_view import model_view
from team_miner.skill_miner.dialogue_dimensions.v2.orchestration import enqueue
from team_miner.skill_miner.dialogue_dimensions.v2.registry import consume
from tests.support.dialogue_dimensions_v2 import MemoryRepository, case, observation, receipt, run_state


@pytest.mark.asyncio
async def test_unknown_post_reconciles_without_reextracting(monkeypatch):
    repo, run = MemoryRepository(), run_state()
    await enqueue(repo, run, "extract", {"case": model_view(case())})
    store, submissions, posts = {}, {}, []
    job = {"job_id": "one", "account": "test", "compiler_body_snapshot": SKILL_BODY, "runtime_timeout_seconds": 3600}

    async def write(engine, job, uri, text):
        store[uri] = text

    async def read(engine, job, uri):
        if uri not in store:
            raise HTTPException(404, "missing")
        return store[uri]

    async def update(engine, job, **fields):
        job.update(fields)
        return job

    async def noop(*args, **kwargs):
        return True

    async def request(account, method, path, **kwargs):
        if path == "/api/v1/fs/ls":
            prefix = kwargs["params"]["uri"] + "/"
            return [{"uri": key} for key in store if key.startswith(prefix)]
        if path.startswith("/api/v1/compile/submissions/"):
            key = path.rsplit("/", 1)[-1]
            if key not in submissions:
                raise HTTPException(404, "absent")
            return {"task_id": submissions[key]}
        if path == "/api/v1/compile":
            key = kwargs["extra_headers"]["Idempotency-Key"]
            posts.append(key)
            submissions[key] = "remote-1"
            packet = (await repo.get("unit-000001"))["packet"]
            store[kwargs["json"]["to"] + "/" + packet["output_path"]] = canonical(receipt(
                packet, {"observations": [observation()], "empty_reason": ""}))
            raise TimeoutError("accepted remotely, acknowledgement lost")
        assert path == "/api/v1/tasks/remote-1"
        return {"task_id": "remote-1", "status": "completed"}

    engine = SimpleNamespace(tenant="alpha", _round_client=lambda job: None, _round_provision_compiler=noop,
                             _knowledge_request=request,
                             admission=SimpleNamespace(acquire=noop, release=noop, mark=noop))
    monkeypatch.setattr(executor, "write", write)
    monkeypatch.setattr(executor, "read", read)
    monkeypatch.setattr(executor, "update", update)
    with pytest.raises(TimeoutError):
        await executor.step(engine, job, repo, run, consume)
    run = await repo.get("run")  # A new process has only PG state.
    await executor.step(engine, job, repo, run, consume)
    await executor.step(engine, job, repo, run, consume)
    assert len(posts) == 1
    assert len(run["evidence"]) == len(run["extracted"]) == 1
    assert (await repo.get("unit-000001"))["status"] == "accepted"
    await executor.step(engine, job, repo, run, consume)
    assert not run["queue"]
    assert len(posts) == 1
