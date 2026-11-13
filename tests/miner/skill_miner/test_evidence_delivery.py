import json

from team_miner.skill_miner.default_compiler import SHA256, SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical, case_key, sha
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan
from team_miner.skill_miner.dialogue_evidence.delivery import build
from team_miner.skill_miner.dialogue_evidence.orchestration import consume
from team_miner.skill_miner.dialogue_evidence.validation import validate
from tests.support.dialogue_evidence import case, receipt


def test_offline_delivery_checksums_sources_zero_and_partial():
    source = case()
    unit = plan(source, DEFAULTS)[0]
    run = {"cases": {case_key(source): source}, "evidence": {}, "issues": [], "metrics": {},
           "dataset_hash": "frozen", "skill_hash": SHA256, "execution_hash": sha(SKILL_BODY),
           "budget_profile": {}, "options": DEFAULTS}
    text = canonical(receipt(unit["packet"]))
    value = validate(text, unit["packet"])
    consume(run, unit, value, sha(text), "job1")
    consume(run, unit, value, sha(text), "job1")
    assert len(run["evidence"]) == 1
    unit.update(status="accepted", receipt=value)
    files, manifest = build({"job_id": "job1"}, run, [unit])
    assert manifest["delivery_status"] == "complete"
    for entry in manifest["files"]:
        assert sha(files[entry["path"]]) == entry["sha256"]
        assert len(files[entry["path"]].encode()) == entry["bytes"]
    exported = json.loads(files["cases-0001.jsonl"])
    for sentence in exported["sentence_index"]:
        assert exported["dialogue_text"][sentence["char_start"]:sentence["char_end"]]
    assert json.loads(files["evidence-0001.jsonl"])["case_version"] == exported["case_version"]
    unit.update(status="failed", receipt={})
    run["evidence"] = {}
    assert build({"job_id": "job1"}, run, [unit])[1]["delivery_status"] == "failed"
    unit.update(status="accepted", receipt=validate(canonical(receipt(unit["packet"], [])), unit["packet"]))
    files, manifest = build({"job_id": "job1"}, run, [unit])
    assert manifest["delivery_status"] == "complete" and files["evidence-0001.jsonl"] == ""
    assert json.loads(files["coverage.jsonl"])["outcome"] == "zero_evidence"
