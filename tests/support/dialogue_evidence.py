"""Constructed Evidence fixtures, unrelated to customer data."""

from team_miner.skill_miner.dialogue_dimensions.input import normalize_case
from team_miner.skill_miner.dialogue_evidence.budget import DEFAULTS, plan


def case(text="HWP：先确认您方便吗？\nC：可以。\nHWP：我们约十分钟。"):
    return normalize_case({"case_ref": "case_" + "a" * 24, "dialogue_text": text})


def task():
    return plan(case(), DEFAULTS)[0]["packet"]


def evidence():
    empty = {"text": "", "sentence_refs": [], "metadata_refs": []}
    return {"local_ref": "e1", "behavior": "先确认交流是否方便", "action_refs": ["s000001"],
            "scenario": empty.copy(), "trigger": empty.copy(), "observed_conditions": [],
            "suggested_applicability": [], "response_refs": ["s000002"], "uncertainties": []}


def receipt(packet, rows=None):
    rows = [evidence()] if rows is None else rows
    return {"schema_version": "taikang.evidence-result.v1",
            **{k: packet[k] for k in ("unit_id", "stage", "input_hash", "case_ref", "case_version")},
            "status": "analyzed", "result": {"evidence": rows, "empty_reason": "纯寒暄" if not rows else ""},
            "issues": []}


class CompileMemoryHost:
    """Synthetic OV files with a malformed child, a field defect and a lost response."""

    def __init__(self):
        self.files, self.submissions, self.tasks = {}, {}, {}
        self.calls = {}
        self.lost_responses = set()
        self.artifacts = {}

    async def noop(self, *args, **kwargs):
        return True

    async def write(self, engine, job, uri, text):
        self.files[uri] = text

    async def read(self, engine, job, uri):
        from starlette.exceptions import HTTPException
        identifier = uri.rsplit("/", 1)[-1]
        if identifier in {"unit-000002.json", "unit-000014.json", "unit-000028.json"} and (
                identifier not in self.lost_responses):
            self.lost_responses.add(identifier)
            raise ConnectionError("synthetic response lost after first unit checkpoint")
        if uri not in self.files:
            raise HTTPException(404, "synthetic output missing")
        return self.files[uri]

    async def update(self, engine, job, **fields):
        job.update(fields)
        return job

    async def artifact(self, engine, job, path, text):
        self.artifacts[path] = text

    async def request(self, account, method, path, **kwargs):
        import json

        from starlette.exceptions import HTTPException

        from team_miner.skill_miner.dialogue_dimensions.input import canonical
        if path == "/api/v1/fs/ls":
            return [{"uri": uri} for uri in self.files if uri.startswith(kwargs["params"]["uri"] + "/")]
        if path.startswith("/api/v1/compile/submissions/"):
            key = path.rsplit("/", 1)[1]
            if key not in self.submissions:
                raise HTTPException(404, "synthetic not submitted")
            return {"task_id": self.submissions[key]}
        if path == "/api/v1/compile":
            key = kwargs["extra_headers"]["Idempotency-Key"]
            if key in self.submissions:
                return {"task_id": self.submissions[key]}
            task_id = "compile-" + str(len(self.tasks) + 1)
            self.submissions[key] = task_id
            request = kwargs["json"]
            packets = [json.loads(text) for uri, text in sorted(self.files.items())
                       if uri.startswith(request["from"][0] + "/")]
            for packet in packets:
                identifier = packet["unit_id"]
                self.calls[identifier] = self.calls.get(identifier, 0) + 1
                value = receipt(packet)
                if identifier == "unit-000027" and self.calls[identifier] == 1:
                    value["result"]["evidence"][0]["trigger"] = {
                        "text": "synthetic late trigger", "sentence_refs": ["s000002"], "metadata_refs": []}
                self.files[request["to"] + "/" + packet["output_path"]] = (
                    '{"truncated"' if identifier == "unit-000013" and self.calls[identifier] == 1
                    else canonical(value))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed",
                "meta": {"pipeline": {"tokens": {"input": len(packets) * 20000, "output": 1000}}}}
            return {"task_id": task_id}
        if path.startswith("/api/v1/tasks/"):
            return self.tasks[path.rsplit("/", 1)[1]]
        raise AssertionError((method, path))
