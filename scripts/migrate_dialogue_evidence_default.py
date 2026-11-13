#!/usr/bin/env python3
"""Explicitly migrate one inspected tenant configuration; never runs at startup."""

import argparse
import json
from types import SimpleNamespace

from team_miner.skill_miner.dialogue_evidence.migration import migrate
from teamEvolver.bootstrap import load_bootstrap
from teamEvolver.database import TransactionManager, get_database_pool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--expected-revision", required=True, type=int)
    args = parser.parse_args()
    config = load_bootstrap()
    if not config.storage_pg_dsn:
        parser.error("PostgreSQL DSN is required")
    pool = get_database_pool(dsn=config.storage_pg_dsn, schema=config.storage_pg_schema, ssl=config.storage_pg_ssl)
    services = SimpleNamespace(tx=TransactionManager(pool), extras={})
    result = pool.run(migrate(services, args.tenant, expected_revision=args.expected_revision))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
