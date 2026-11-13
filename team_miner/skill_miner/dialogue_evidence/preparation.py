"""Read and verify immutable raw sources; never read previous analysis."""

import json

from ..dialogue_dimensions.input import canonical, case_key, parse_model, sha
from ..dialogue_dimensions.orchestration import frozen_read
from ..dialogue_dimensions.transport import descendant


async def load_cases(engine, job):
    raw = await frozen_read(engine, job, job["dataset_manifest_ref"])
    if sha(raw) != job["dataset_manifest_sha256"]:
        raise ValueError("dataset_manifest_changed")
    manifest = json.loads(raw)
    expected = manifest.pop("dataset_sha256", "")
    if expected != sha(canonical(manifest)) or manifest.get("schema_version") != "taikang.dialogue-dataset.v1":
        raise ValueError("invalid_dataset_manifest")
    root = job["dataset_manifest_ref"].rsplit("/", 1)[0]
    cases, issues = {}, []
    for page in manifest["pages"]:
        entry = page["manifest"]
        text = await frozen_read(engine, job, descendant(root, entry["path"]))
        if sha(text) != entry["sha256"]:
            raise ValueError("dataset_page_changed")
        for line in text.splitlines():
            item = json.loads(line)
            uri = descendant(root, item["path"])
            if not uri.startswith(job["input_dir"].rstrip("/") + "/"):
                raise ValueError("case_outside_source")
            content = await frozen_read(engine, job, uri)
            if sha(content) != item["sha256"]:
                raise ValueError("frozen_case_changed")
            case = parse_model(content)
            if any(case[key] != item[key] for key in ("case_ref", "case_version")) or case_key(case) in cases:
                raise ValueError("duplicate_or_changed_case")
            cases[case_key(case)] = case
        entry = page["issues"]
        text = await frozen_read(engine, job, descendant(root, entry["path"]))
        if sha(text) != entry["sha256"]:
            raise ValueError("dataset_issues_changed")
        issues.extend(json.loads(line) for line in text.splitlines())
    if len(cases) != manifest["case_count"]:
        raise ValueError("dataset_case_count_mismatch")
    return cases, issues, expected
