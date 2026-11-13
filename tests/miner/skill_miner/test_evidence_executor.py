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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rate_limit", "capacity", "malformed"])
async def test_exhausted_repair_salvages_original_partial_receipt_and_replays_its_uri(monkeypatch, failure):
    from team_miner.skill_miner.dialogue_evidence.validation import validate
    source = case()
    unit = plan(source, DEFAULTS)[0]
    packet = unit["packet"]
    raw = receipt(packet)
    raw["result"]["evidence"][0]["trigger"] = {
        "text": "事后回应", "sentence_refs": ["s000002"], "metadata_refs": []}
    text = canonical(raw)
    from team_miner.skill_miner.dialogue_dimensions.input import sha
    fallback_uri = "viking://resources/first/receipt.json"
    unit.update(status="retry_wait", attempt=2, transport_attempt=5, processed_task_id="first",
                fallback={"receipt": validate(text, packet), "receipt_hash": sha(text),
                          "task_id": "first", "uri": fallback_uri})
    active = {"key": "repair-key", "task_id": "repair", "units": [packet["unit_id"]],
              "submitted_at": "2026-11-13T00:00:00+00:00",
              "request": {"from": ["viking://resources/repair-input"], "to": "viking://resources/repair"}}
    run = {"queue": [packet["unit_id"]], "active": active, "skill_hash": SHA256,
           "dataset_hash": "frozen", "options": dict(DEFAULTS), "issues": [], "evidence": {},
           "metrics": {"compiles": 1, "accepted": 0, "retries": 1}}
    repo = MemoryRepository()
    await repo.save(run, **{packet["unit_id"]: unit})
    reads = []

    async def read(engine, job, uri):
        reads.append(uri)
        if uri == fallback_uri:
            return text
        if uri.endswith(".md"):
            return canonical(packet)
        raise HTTPException(404, "missing")

    async def request(account, method, path, **kwargs):
        return {"task_id": "repair", "status": "failed",
                "error": "rate_limit_exceeded" if failure == "rate_limit" else (
                    "context_length" if failure == "capacity" else "invalid child JSON")}

    async def noop(*args, **kwargs):
        return True

    async def update(engine, job, **fields):
        job.update(fields)
        return job

    engine = SimpleNamespace(tenant="test", _knowledge_request=request,
                             admission=SimpleNamespace(release=noop))
    job = {"job_id": "job", "account": "test", "runtime_timeout_seconds": 3600}
    monkeypatch.setattr(executor, "read", read)
    monkeypatch.setattr(executor, "update", update)
    await executor.step(engine, job, repo, run, consume)
    saved = await repo.get(packet["unit_id"])
    assert saved["status"] == "accepted" and saved["receipt_uri"] == fallback_uri
    assert saved["receipt"]["quarantined"] and len(run["evidence"]) == 1
    # Replay after the unit commit but before the Compile-clear checkpoint.
    run["active"] = active
    await repo.save(run)
    await executor.step(engine, job, repo, run, consume)
    assert fallback_uri in reads and run["metrics"]["accepted"] == 1
