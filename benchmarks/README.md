# Benchmarks

## audit_throughput.py

Wall time for N concurrent audit appends (default 50) against Postgres, repeated
(default 5) with min, median and max seconds and milliseconds per append. The script
uses only API present in 0.1.0a3 and detects later features (`append_many`, an async
`aclose` on the database), so the same file measures the before and the after.
Afterwards it runs `verify()` and fails unless the chain head seq equals the number of
appended records.

Each run creates a fresh database with owner, requester and approver roles, logs as
the requester role, and drops the database at the end.

**The test Postgres runs on tmpfs, so commit (fsync) cost is understated.** Compare
runs on the same instance; do not read the numbers as production latency.

```bash
export AGENT_CORE_TEST_POSTGRES_ADMIN_URL=postgresql://postgres@127.0.0.1:4202/postgres

# after: from a checkout of the new code
uv run python benchmarks/audit_throughput.py --json after.json

# before: run this file from the v0.1.0a3 checkout's environment
git worktree add --detach ../a3 v0.1.0a3 && (cd ../a3 && uv sync)
(cd ../a3 && uv run python "$OLDPWD/benchmarks/audit_throughput.py" --json before.json)
```

Options: `--count N`, `--repeat N`, `--json PATH`.
