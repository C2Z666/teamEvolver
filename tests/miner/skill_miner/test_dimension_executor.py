"""Lost acknowledgements and failed children use durable, per-unit recovery."""

from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.dialogue_dimensions.input import canonical
from team_miner.skill_miner.dialogue_dimensions.v2 import executor
from team_miner.skill_miner.dialogue_dimensions.v2.input_view import model_view
from team_miner.skill_miner.dialogue_dimensions.v2.orchestration import enqueue
from team_miner.skill_miner.dialogue_dimensions.v2.registry import consume
from team_miner.skill_miner.dialogue_dimensions.v2.resource import SKILL_BODY
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


@pytest.mark.asyncio
async def test_remote_rate_limit_backs_off_without_spending_semantic_repairs(monkeypatch):
    from team_miner.skill_miner.mining_contracts import utc_now

    repo, run = MemoryRepository(), run_state()
    await enqueue(repo, run, "extract", {"case": model_view(case())})
    packet = (await repo.get("unit-000001"))["packet"]
    run["active"] = {"task_id": "limited", "key": "original-key", "units": ["unit-000001"],
                     "submitted_at": utc_now(), "request": {"from": ["viking://resources/in"],
                                                           "to": "viking://resources/out"}}
    job = {"job_id": "one", "account": "test", "compiler_body_snapshot": SKILL_BODY,
           "runtime_timeout_seconds": 3600}
    posts, store = [], {}

    async def read(engine, job, uri):
        if uri == "viking://resources/in/unit-000001.md":
            return canonical(packet)
        if uri in store:
            return store[uri]
        raise HTTPException(404, "missing output")

    async def write(engine, job, uri, text):
        store[uri] = text

    async def request(account, method, path, **kwargs):
        if path == "/api/v1/tasks/limited":
            return {"task_id": "limited", "status": "failed",
                    "error": "COMPILE_INCOMPLETE: Error code: 429 ModelAccountTpmRateLimitExceeded"}
        if path == "/api/v1/fs/ls":
            return [{"uri": key} for key in store if key.startswith(kwargs["params"]["uri"] + "/")]
        if path.startswith("/api/v1/compile/submissions/"):
            raise HTTPException(404, "absent")
        if path == "/api/v1/compile":
            assert "ModelAccountTpmRateLimitExceeded" not in kwargs["json"]["instruction"]
            assert "Repair failed assignments" not in kwargs["json"]["instruction"]
            posts.append(kwargs["extra_headers"]["Idempotency-Key"])
            return {"task_id": "retry-transport"}
        raise AssertionError(path)

    async def update(engine, job, **fields):
        job.update(fields)
        return job

    async def noop(*args, **kwargs):
        return True

    engine = SimpleNamespace(tenant="alpha", _round_client=lambda job: None, _round_provision_compiler=noop,
                             _knowledge_request=request,
                             admission=SimpleNamespace(acquire=noop, release=noop, mark=noop))
    monkeypatch.setattr(executor, "read", read)
    monkeypatch.setattr(executor, "write", write)
    monkeypatch.setattr(executor, "update", update)
    await executor.step(engine, job, repo, run, consume)
    unit = await repo.get("unit-000001")
    assert unit["attempt"] == 0 and unit["transport_attempt"] == 1
    assert run["metrics"]["retries"] == 0 and run["metrics"]["transport_retries"] == 1
    await executor.step(engine, job, repo, run, consume)
    assert not posts  # No hot retry while the account is rate-limited.
    run["compile_not_before"] = "2000-01-01T00:00:00+00:00"
    await executor.step(engine, job, repo, run, consume)
    assert len(posts) == 1 and posts[0] != "original-key"
    assert run["active"]["task_id"] == "retry-transport"
    assert (await repo.get("unit-000001"))["attempt"] == 0


@pytest.mark.asyncio
async def test_remote_rate_limit_has_finite_transport_budget(monkeypatch):
    from team_miner.skill_miner.mining_contracts import utc_now

    repo, run = MemoryRepository(), run_state()
    await enqueue(repo, run, "extract", {"case": model_view(case())})
    unit = await repo.get("unit-000001")
    unit["transport_attempt"] = run["options"]["transport_retry_attempts"]
    await repo.save(run, **{"unit-000001": unit})
    run["active"] = {"task_id": "limited", "key": "key", "units": ["unit-000001"],
        "submitted_at": utc_now(), "request": {"from": ["viking://resources/in"], "to": "viking://resources/out"}}

    async def read(engine, job, uri):
        if uri.startswith("viking://resources/in/"):
            return canonical(unit["packet"])
        raise HTTPException(404, "missing")

    async def request(*args, **kwargs):
        return {"task_id": "limited", "status": "failed", "error": "Error code: 429 TooManyRequests"}

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(executor, "read", read)
    monkeypatch.setattr(executor, "update", noop)
    engine = SimpleNamespace(_knowledge_request=request, admission=SimpleNamespace(release=noop))
    await executor.step(engine, {"account": "test"}, repo, run, consume)
    assert (await repo.get("unit-000001"))["status"] == "deferred"
    assert run["issues"][-1]["reason"] == "upstream_rate_limit_exhausted"
    assert run["metrics"]["retries"] == 0
