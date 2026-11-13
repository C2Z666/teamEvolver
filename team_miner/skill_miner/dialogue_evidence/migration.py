"""Explicit once-only, tenant-targeted S3 default migration with transactional backup."""

import copy
import json
from pathlib import Path

from .. import config
from ..default_compiler import SHA256
from ..dialogue_dimensions.input import sha

PREVIOUS_DEFAULT = (Path(__file__).parents[1] / "compiler_skills" /
                    "taikang-dialogue-dimensions-s2" / "SKILL.md").read_text(encoding="utf-8")

MIGRATION = "taikang-dialogue-evidence-s3-default-v1"


async def migrate(services, tenant, *, expected_revision):
    table = services.tx.pool.schema + ".dialogue_dimension_state"
    async with services.tx.tenant_tx(tenant) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", tenant + ":" + MIGRATION)
        prior = await conn.fetchrow(
            f"SELECT document FROM {table} WHERE tenant_id=$1 AND job_id='configuration' AND state_key=$2",
            tenant, MIGRATION)
        if prior:
            return {"status": "already_applied"}
        current = await config.load(services, conn)
        if current.revision != expected_revision:
            from ..repository import MiningRevisionConflict
            raise MiningRevisionConflict("Target settings changed; inspect the current revision before migration")
        before = (current.config.get("mining", {}).get("prompts", {}).get("ov_compile") or PREVIOUS_DEFAULT)
        value = copy.deepcopy(current.config)
        mining = value.setdefault("mining", {})
        mining.setdefault("prompts", {}).pop("ov_compile", None)
        mining["s3_migration"] = MIGRATION
        saved = await config.save(services, conn, value, expected_revision=expected_revision)
        audit = {"migration": MIGRATION, "previous_document": current.config, "previous_effective_prompt": before,
                 "previous_sha256": sha(before), "previous_revision": current.revision,
                 "new_revision": saved.revision, "default_sha256": SHA256}
        await conn.execute(f"INSERT INTO {table}(tenant_id,job_id,state_key,document) "
                           "VALUES($1,'configuration',$2,$3::jsonb)", tenant, MIGRATION, json.dumps(audit))
        return {"status": "applied", "revision": saved.revision, "default_sha256": SHA256}
