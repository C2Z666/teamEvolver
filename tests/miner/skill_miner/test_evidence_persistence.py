"""Real PG CAS, tenant isolation and one-time configuration migration."""

from types import SimpleNamespace

import pytest

from team_miner.skill_miner import config
from team_miner.skill_miner.default_compiler import SHA256, SKILL_BODY
from team_miner.skill_miner.dialogue_evidence.migration import migrate
from team_miner.skill_miner.dialogue_evidence.repository import Repository, RevisionConflict
from team_miner.skill_miner.settings import compiler_prompt

pytestmark = pytest.mark.postgres


def test_atomic_receipt_cas_and_tenant_isolation(pg_env):
    async def exercise():
        async def guard(conn):
            pass
        services = SimpleNamespace(tx=pg_env.tx)
        engine = SimpleNamespace(services=services, tenant="alpha", _guard_connection=guard)
        first, stale = Repository(engine, {"job_id": "one"}), Repository(engine, {"job_id": "one"})
        await first.save({"registry_revision": 0})
        await stale.get("run")
        await first.save({"registry_revision": 1}, unit={"status": "accepted"})
        with pytest.raises(RevisionConflict):
            await stale.save({"registry_revision": 2}, unit={"status": "corrupt"})
        assert await first.get("unit") == {"status": "accepted"}
        other = Repository(SimpleNamespace(services=services, tenant="beta"), {"job_id": "one"})
        assert await other.get("run") is None
        async def lost(conn):
            raise RuntimeError("worker fence expired")
        engine._guard_connection = lost
        with pytest.raises(RuntimeError, match="fence"):
            await first.save({"registry_revision": 3})
        assert (await first.get("run"))["registry_revision"] == 1
    pg_env.tx.pool.run(exercise())


def test_migration_is_transactional_once_only_and_retains_custom_edits(pg_env):
    async def exercise():
        services = SimpleNamespace(tx=pg_env.tx, extras={})
        async with pg_env.tx.tenant_tx("alpha") as conn:
            saved = await config.save(services, conn, {"mining": {"prompts": {"ov_compile": "old business body"},
                                                               "unrelated": "keep"}}, expected_revision=0)
        result = await migrate(services, "alpha", expected_revision=saved.revision)
        assert result["default_sha256"] == SHA256
        async with pg_env.tx.tenant_tx("alpha") as conn:
            current = await config.load(services, conn)
            assert compiler_prompt(current.config) == SKILL_BODY
            assert current.config["mining"]["unrelated"] == "keep"
            current.config["mining"]["prompts"]["ov_compile"] = SKILL_BODY.replace(
                "<!-- s3:common:end -->", "custom later edit\n<!-- s3:common:end -->")
            await config.save(services, conn, current.config, expected_revision=current.revision)
        assert (await migrate(services, "alpha", expected_revision=0))["status"] == "already_applied"
        async with pg_env.tx.tenant_tx("alpha") as conn:
            current = await config.load(services, conn)
            assert "custom later edit" in compiler_prompt(current.config)
        async with pg_env.tx.tenant_tx("beta") as conn:
            assert (await config.load(services, conn)).revision == 0
    pg_env.tx.pool.run(exercise())
