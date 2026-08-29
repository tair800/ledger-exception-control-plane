-- Least-privilege application role.
--
-- Deliberately NOT an Alembic migration. A role is a cluster-level object while a migration
-- operates on one database, so role DDL in a migration is wrong in both directions: it leaks
-- outside the database being migrated, and it fails on a managed platform where the migrating
-- user cannot create roles. Deployment order is therefore: run migrations, then run this.
--
-- Idempotent: safe to run on every release.
--
-- The role is created NOLOGIN. Whether it can log in, and with what credential, is a
-- deployment decision (increment 10.1) and no password appears in source control. Grant LOGIN
-- and set the credential out of band:
--     ALTER ROLE lecp_app LOGIN PASSWORD '<from the platform secret store>';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lecp_app') THEN
        CREATE ROLE lecp_app NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO lecp_app;

-- Ordinary tables: full DML, no DDL. The application never migrates itself.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lecp_app;

-- audit_event is append-only. Revoke first: ALL TABLES above granted UPDATE and DELETE, and a
-- grant is not undone by a narrower grant issued afterwards.
--
-- This is defence in depth, not the primary control. The primary control is the
-- audit_event_append_only_* trigger, which applies to every role including the table owner --
-- and the owner is precisely the role a migration or a maintenance script runs as, where a
-- grant offers no protection at all.
REVOKE ALL ON TABLE audit_event FROM lecp_app;
GRANT SELECT, INSERT ON TABLE audit_event TO lecp_app;

-- Sequences: none today (all primary keys are application-generated UUIDs). Included so a
-- future sequence does not silently break the application on the first release that adds one.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lecp_app;
