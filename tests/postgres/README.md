# Test Postgres

Local Postgres for the audit-log tests. Loopback only (127.0.0.1:4202), trust auth, no passwords, data is ephemeral.

Start it from the repo root:

    docker compose up -d --wait postgres

Point the tests at it:

    export AGENT_CORE_TEST_POSTGRES_ADMIN_URL=postgresql://postgres@127.0.0.1:4202/postgres

The tests create and drop their own database per session. Without the variable the Postgres tests are skipped, unless `AGENT_CORE_REQUIRE_POSTGRES=1` is set (CI sets it).

Stop it:

    docker compose down
