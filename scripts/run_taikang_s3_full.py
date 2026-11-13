"""Submit the registered 171-case S3 source once through the authenticated local API."""

from __future__ import annotations

import argparse
import os
import time
from urllib.parse import quote, urlparse

import httpx

from team_miner.skill_miner.default_compiler import SHA256

SOURCE_ID = "ovsrc-660e80ccd41f3f673679"
INPUT_DIR = "viking://resources/taikang-dialogue-dimensions-171-20261110-68a6feda/model_cases"
IDEMPOTENCY_KEY = "taikang-s3-full-171-adaptive-20261113-v1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:52010")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--idempotency-key", default=IDEMPOTENCY_KEY)
    args = parser.parse_args()
    if urlparse(args.api).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("This operator script only sends service credentials to the local API.")
    root_key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
    if not root_key:
        parser.error("Load the existing project environment containing TEAMEVOLVER_ROOT_API_KEY.")
    headers = {"Authorization": "Bearer " + root_key, "X-Tenant-Id": args.tenant}
    with httpx.Client(base_url=args.api, headers=headers, timeout=60) as client:
        settings = client.get("/api/mining/settings")
        settings.raise_for_status()
        if settings.json().get("execution_protocol") != "te-dialogue-evidence.v1":
            parser.error("The selected tenant must use the S3 Evidence compiler.")
        if settings.json().get("effective_sha256") != SHA256:
            parser.error("The API compiler does not match this checkout; load the verified implementation first.")
        body = {"name": "泰康 S3 行为 Evidence 171 条全量 · 稳健自适应",
                "source_id": SOURCE_ID, "input_dir": INPUT_DIR, "max_rounds": 1,
                "runtime_timeout_seconds": 3600}
        accepted = client.post("/api/mining/jobs", json=body,
                               headers={"Idempotency-Key": args.idempotency_key})
        accepted.raise_for_status()
        identifier = accepted.json()["job_id"]
        print("Accepted operation:", identifier, flush=True)
        print("Idempotency key:", args.idempotency_key, flush=True)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = client.get("/api/mined-jobs/" + quote(identifier, safe=""))
            response.raise_for_status()
            operation = response.json()
            if operation["status"] == "succeeded":
                jobs = (operation.get("result") or {}).get("jobs", [])
                if not jobs:
                    raise RuntimeError("Operation succeeded without a job result; inspect its status before retrying.")
                for job in jobs:
                    print("Job:", job["job_id"], "status:", job.get("status", ""), flush=True)
                print("Viewer: http://127.0.0.1:52131/", flush=True)
                return
            if operation["status"] in {"failed", "cancelled"}:
                raise RuntimeError("Operation " + operation["status"] + ": " + operation.get("error_code", ""))
            time.sleep(2)
        raise RuntimeError("Acceptance is still pending. Inspect " + identifier + "; reuse the same key if rerunning.")


if __name__ == "__main__":
    main()
