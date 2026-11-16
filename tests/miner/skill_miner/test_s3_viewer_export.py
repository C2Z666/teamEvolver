"""Portable export preserves delivery bytes and the job's frozen Skill."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
VIEWER = ROOT / "my-doc/泰康-对话打分优化/develop/S3/展示界面"
SPEC = importlib.util.spec_from_file_location(
    "s3_export_viewer", VIEWER / "backend/__init__.py", submodule_search_locations=[str(VIEWER / "backend")])
PACKAGE = importlib.util.module_from_spec(SPEC)
sys.modules["s3_export_viewer"] = PACKAGE
SPEC.loader.exec_module(PACKAGE)
from s3_export_viewer.exporter import DEFAULT_EXPORT_ROOT, export_bundle, relative_file  # noqa: E402

from team_miner.skill_miner.dialogue_dimensions.input import normalize_case  # noqa: E402
from team_miner.skill_miner.dialogue_evidence.compiler import stage_body  # noqa: E402
from team_miner.skill_miner.dialogue_evidence.delivery import build  # noqa: E402
from team_miner.skill_miner.ov_round_orchestration_helpers import _compiler_package  # noqa: E402


def snapshot():
    body = (ROOT / "team_miner/skill_miner/compiler_skills/taikang-dialogue-evidence-s3/SKILL.md").read_text()
    case = normalize_case({"case_ref": "case_" + "b" * 24, "dialogue_text": "HWP：你好😀。\nC：谢谢。"})
    run = {"cases": {"case": case}, "evidence": {}, "issues": [], "stage": "finished", "metrics": {},
           "budget_profile": {}, "dataset_hash": "d", "options": {},
           "skill_hash": hashlib.sha256(body.encode()).hexdigest(),
           "execution_hash": hashlib.sha256(_compiler_package(stage_body(body, "extract"))[1].encode()).hexdigest()}
    unit = {"packet": {"unit_id": "unit-000001", "case_ref": case["case_ref"],
                       "case_version": case["case_version"], "payload": {"scope": {
                           "owned_sentence_ids": [s["sentence_id"] for s in case["sentence_index"]]}}},
            "status": "accepted", "receipt": {"quarantined": [], "issues": [], "raw_evidence_count": 0}}
    job = {"job_id": "job-1", "status": "succeeded", "compiler_body_snapshot": body}
    files, _ = build(job, run, [unit])
    return job, {"run": run, "unit-000001": unit}, {
        "semantic_reports/dialogue_dimensions/" + k: v.encode() for k, v in files.items()}


def test_portable_export_bytes_skill_and_no_overwrite(tmp_path):
    job, records, files = snapshot()
    runtime = lambda *args, **kwargs: {"compiles": [], "units": []}
    first = export_bundle(job, records, files, {}, str(tmp_path), VIEWER, runtime)
    second = export_bundle(job, records, files, {}, str(tmp_path), VIEWER, runtime)
    assert first["directory"] != second["directory"]
    folder = Path(first["directory"])
    assert DEFAULT_EXPORT_ROOT == ROOT / "exports"
    for path, content in files.items():
        assert (folder / "output" / path.split("dialogue_dimensions/", 1)[1]).read_bytes() == content
    assert (folder / "skill/SKILL.md").read_text() == job["compiler_body_snapshot"]
    assert 'src="assets/data.js"' in (folder / "index.html").read_text()
    assert 'src="/app.js"' not in (folder / "index.html").read_text()
    for line in (folder / "SHA256SUMS.txt").read_text().splitlines():
        digest, path = line.split("  ", 1)
        assert hashlib.sha256((folder / path).read_bytes()).hexdigest() == digest
    assert json.loads((folder / "导出核对.json").read_text())["manifest_verified"]
    assert not list(tmp_path.glob(".s3-export-*"))


def test_mismatched_skill_fails_without_partial_export(tmp_path):
    job, records, files = snapshot()
    job["compiler_body_snapshot"] += "changed"
    with pytest.raises(ValueError, match="冻结 Skill"):
        export_bundle(job, records, files, {}, str(tmp_path), VIEWER, lambda *args, **kwargs: {})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["../outside", "/absolute", "x/../../outside", "x\\outside"])
def test_export_rejects_unsafe_artifact_paths(name):
    with pytest.raises(ValueError, match="不安全路径"):
        relative_file(name)
