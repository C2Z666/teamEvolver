#!/usr/bin/env python3
"""Start S3 from a registered prepared raw-case source using the durable Worker API."""

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

from team_miner.skill_miner import config
from team_miner.skill_miner.compiler_protocol import detect
from team_miner.skill_miner.dialogue_evidence import PROTOCOL
from team_miner.skill_miner.settings import compiler_prompt
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
        if detect(compiler_prompt(settings.config)) != PROTOCOL:
            raise ValueError("Target effective compiler is not Evidence v1")
        if not args.source_uri.endswith("/model_cases"):
            raise ValueError("This verification command requires prepared raw model_cases")
        ov = await effective(services, args.tenant)
        settings = replace(settings, miner_config={**settings.miner_config,
            "account_id": settings.miner_config.get("account_id") or ov.sharing_viking_account,
            "user_id": settings.miner_config.get("user_id") or ov.sharing_viking_user})
        request = SimpleNamespace(headers={"Idempotency-Key": args.key},
            state=SimpleNamespace(console_user={"id": "evidence-runtime-verification"}))
        result = await accept(request, services, "job.create", {
            "input_dir": args.source_uri, "source_id": args.source_id, "name": args.name,
            "runtime_timeout_seconds": 3600, "human_checkpoints": False}, settings)
        print(json.dumps(result, ensure_ascii=False))
        print("job_id=ovmine-" + hashlib.sha256(result["job_id"].encode()).hexdigest()[:32])
    finally:
        reset_current_tenant(token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tenant", "source-uri", "source-id", "name", "key"):
        parser.add_argument("--"+name, required=True)
    asyncio.run(submit(parser.parse_args()))


if __name__ == "__main__":
    main()
