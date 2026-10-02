-- The owner role owns the audit tables; the app role may only INSERT and
-- SELECT on the audit table, which the library checks on connect.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_core_owner') THEN
        CREATE ROLE agent_core_owner LOGIN NOSUPERUSER CREATEDB;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_core_app') THEN
        CREATE ROLE agent_core_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;
