# Test Postgres

Local Postgres for the audit-log tests. Loopback only (127.0.0.1:4202), trust auth, no passwords, data is ephemeral.

Start it from the repo root:

    docker compose up -d --wait postgres

Point the tests at it:

    export AGENT_CORE_TEST_POSTGRES_ADMIN_URL=postgresql://postgres@127.0.0.1:4202/postgres

Each test run names its roles and databases with its own id (printed in the pytest header), creates them as it needs them and drops them when the session ends, so two runs can share one server at the same time. Roles are server-wide, which is why the names carry the id. Without the variable the Postgres tests are skipped, unless `AGENT_CORE_REQUIRE_POSTGRES=1` is set (CI sets it).

Stop it:

    docker compose down
