#!/usr/bin/env python3
"""Submit raw whitelist JSONL through the normal durable job acceptance path."""

import argparse
import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from team_miner.skill_miner import config
from team_miner.skill_miner.task_io import accept
from teamEvolver.app import build_role_services
from teamEvolver.bootstrap import load_bootstrap
from teamEvolver.database import TransactionManager, get_database_pool
from teamEvolver.openviking.settings import effective
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant


async def submit(args):
    bootstrap = load_bootstrap()
    pool = get_database_pool(dsn=bootstrap.storage_pg_dsn, schema=bootstrap.storage_pg_schema,
                             ssl=bootstrap.storage_pg_ssl)
    services = build_role_services(role="api", tx=TransactionManager(pool), config=bootstrap)
    token = set_current_tenant(TenantContext(tenant_id=args.tenant))
    try:
        async with services.tx.tenant_tx(args.tenant) as conn:
            settings = await config.load(services, conn)
        ov = await effective(services, args.tenant)
        settings = replace(settings, miner_config={**settings.miner_config,
            "account_id": settings.miner_config.get("account_id") or ov.sharing_viking_account,
            "user_id": settings.miner_config.get("user_id") or ov.sharing_viking_user})
        raw = Path(args.cases).read_bytes()
        request = SimpleNamespace(headers={"Idempotency-Key": args.key},
                                  state=SimpleNamespace(console_user={"id": "s2-runtime-verification"}))
        result = await accept(request, services, "job.create", {
            "input_dir": args.source_uri, "source_id": args.source_id, "name": args.name,
            "runtime_timeout_seconds": 3600,
            "dialogue_input": {"format": "jsonl", "filename": Path(args.cases).name,
                               "content_base64": base64.b64encode(raw).decode()}}, settings)
        print(json.dumps(result, ensure_ascii=False))
        # The task's stable operation identity deterministically names the business job.
        identifier = result.get("job_id", "")
        if identifier:
            print("job_id=ovmine-" + hashlib.sha256(identifier.encode()).hexdigest()[:32])
    finally:
        reset_current_tenant(token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("tenant", "source-uri", "source-id", "cases", "name", "key"):
        parser.add_argument("--" + option, required=True)
    asyncio.run(submit(parser.parse_args()))


if __name__ == "__main__":
    main()
