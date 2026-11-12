"""Atomic run/receipt transitions with PG CAS and the current Worker fence."""

import json


class RevisionConflict(RuntimeError):
    pass


class Repository:
    def __init__(self, engine, job):
        self.engine, self.job = engine, job
        self.table = engine.services.tx.pool.schema + ".dialogue_dimension_state"
        self.revision = 0

    async def get(self, key):
        async with self.engine.services.tx.tenant_tx(self.engine.tenant) as conn:
            row = await conn.fetchrow(
                f"SELECT revision,document FROM {self.table} WHERE tenant_id=$1 AND job_id=$2 AND state_key=$3",
                self.engine.tenant, self.job["job_id"], key)
        if key == "run":
            self.revision = int(row["revision"]) if row else 0
        if not row:
            return None
        return json.loads(row["document"]) if isinstance(row["document"], str) else dict(row["document"])

    async def save(self, run, **records):
        async with self.engine.services.tx.tenant_tx(self.engine.tenant) as conn:
            await self.engine._guard_connection(conn)
            args = (self.engine.tenant, self.job["job_id"], json.dumps(run, ensure_ascii=False))
            if not self.revision:
                value = await conn.fetchval(
                    f"INSERT INTO {self.table}(tenant_id,job_id,state_key,document) VALUES($1,$2,'run',$3::jsonb) "
                    "ON CONFLICT DO NOTHING RETURNING revision", *args)
            else:
                value = await conn.fetchval(
                    f"UPDATE {self.table} SET document=$3::jsonb,revision=revision+1,updated_at=now() "
                    "WHERE tenant_id=$1 AND job_id=$2 AND state_key='run' AND revision=$4 RETURNING revision",
                    *args, self.revision)
            if value is None:
                raise RevisionConflict("S2 run changed; reload the frozen registry before applying a decision")
            for key, document in records.items():
                await conn.execute(
                    f"INSERT INTO {self.table}(tenant_id,job_id,state_key,document) VALUES($1,$2,$3,$4::jsonb) "
                    "ON CONFLICT(tenant_id,job_id,state_key) DO UPDATE SET document=EXCLUDED.document,"
                    f"revision={self.table}.revision+1,updated_at=now()",
                    self.engine.tenant, self.job["job_id"], key, json.dumps(document, ensure_ascii=False))
        self.revision = int(value)
