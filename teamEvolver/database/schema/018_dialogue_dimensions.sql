-- S2 state, accepted units and deployment migration audit remain tenant scoped.
CREATE TABLE {schema}.dialogue_dimension_state (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    state_key TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, job_id, state_key)
);
ALTER TABLE {schema}.dialogue_dimension_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.dialogue_dimension_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.dialogue_dimension_state
    USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
