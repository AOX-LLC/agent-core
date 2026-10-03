"""verify_report: every problem in the chain, not the first, and where the walk goes on."""

import pytest

from aox_agent_core.audit import VerifyReport
from aox_agent_core.audit import sql as audit_sql
from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.errors import AuditIntegrityError
from databases import ControlDatabase
from test_audit import _rewrite_actor_and_rehash, drop_triggers, event, filled_log

TABLE = audit_sql.AUDIT_TABLE


def kinds(report: VerifyReport) -> list[tuple[int | None, str]]:
    return [(problem.seq, problem.kind) for problem in report.problems]


async def test_a_clean_log_reports_ok_and_the_same_head_verify_returns(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 5)

    report = await log.verify_report()

    assert report.ok
    assert report.problems == ()
    assert not report.truncated
    assert report.records_checked == 5
    assert report.head == await log.verify() == await log.head()


async def test_an_empty_log_is_ok_at_the_genesis_head(control_database: ControlDatabase) -> None:
    log = SQLAuditLog(control_database.database)

    report = await log.verify_report()

    assert report.ok
    assert report.head.seq == 0
    assert report.records_checked == 0


async def test_two_altered_records_are_both_reported_where_verify_stops_at_the_first(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 6)
    drop_triggers(control_database)
    control_database.raw(f"UPDATE {TABLE} SET actor_id = 'someone-else' WHERE seq IN (2, 5)")

    report = await log.verify_report()

    assert kinds(report) == [(2, "hash"), (5, "hash")]
    assert not report.ok
    assert report.records_checked == 6
    assert report.head.seq == 6  # it walked to the end
    with pytest.raises(AuditIntegrityError, match="Record 2"):
        await log.verify()


async def test_a_missing_record_is_one_problem_not_a_cascade(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 6)
    drop_triggers(control_database)
    control_database.raw(f"DELETE FROM {TABLE} WHERE seq = 3")

    report = await log.verify_report()

    assert kinds(report) == [(4, "seq")]
    assert "Records 3 to 3 are missing" in report.problems[0].detail
    assert report.head.seq == 6


async def test_a_record_whose_link_was_cut_is_reported_once_at_that_record(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 5)
    drop_triggers(control_database)
    control_database.raw(f"UPDATE {TABLE} SET prev_hash = '{'f' * 64}' WHERE seq = 4")

    report = await log.verify_report()

    assert (4, "link") in kinds(report)
    assert all(problem.seq in (4, 5) for problem in report.problems)
    assert report.records_checked == 5


async def test_an_unreadable_record_is_reported_and_the_walk_goes_on(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 5)
    drop_triggers(control_database)
    control_database.raw(f"UPDATE {TABLE} SET payload = 'not json' WHERE seq = 3")
    control_database.raw(f"UPDATE {TABLE} SET actor_id = 'someone-else' WHERE seq = 5")

    report = await log.verify_report()

    assert kinds(report) == [(3, "malformed"), (5, "hash")]
    assert report.head.seq == 5


async def test_a_cut_tail_and_a_rewritten_chain_show_only_against_an_anchor(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 4)
    anchor = await log.head()
    drop_triggers(control_database)
    _rewrite_actor_and_rehash(control_database, seq=2, actor_id="someone-else")

    assert (await log.verify_report()).ok  # the rebuilt chain is self-consistent
    report = await log.verify_report(expected_head=anchor)
    assert kinds(report) == [(4, "anchor")]
    assert "rewritten" in report.problems[0].detail

    control_database.raw(f"DELETE FROM {TABLE} WHERE seq = 4")
    cut = await log.verify_report(expected_head=anchor)
    assert kinds(cut) == [(None, "anchor")]
    assert "removed from the end" in cut.problems[0].detail


async def test_the_report_stops_at_max_problems_and_says_so(
    control_database: ControlDatabase,
) -> None:
    log = await filled_log(control_database, 6)
    drop_triggers(control_database)
    control_database.raw(f"UPDATE {TABLE} SET actor_id = 'someone-else'")

    report = await log.verify_report(max_problems=2)

    assert len(report.problems) == 2
    assert report.truncated
    assert not report.ok


async def test_a_walk_across_batches_finds_problems_in_every_batch(
    control_database: ControlDatabase,
) -> None:
    log = SQLAuditLog(control_database.database)
    total = audit_sql.READ_BATCH_SIZE * 2 + 40
    for start in range(1, total + 1, 500):
        await log.append_many([event(n) for n in range(start, min(start + 500, total + 1))])
    drop_triggers(control_database)
    bad = (3, audit_sql.READ_BATCH_SIZE + 7, total)
    control_database.raw(f"UPDATE {TABLE} SET actor_id = 'someone-else' WHERE seq IN {bad}")

    report = await log.verify_report()

    assert [problem.seq for problem in report.problems] == list(bad)
    assert report.records_checked == total


async def test_the_report_changes_nothing(control_database: ControlDatabase) -> None:
    log = await filled_log(control_database, 4)
    before = control_database.raw(f"SELECT seq, record_hash FROM {TABLE} ORDER BY seq")

    await log.verify_report()

    assert control_database.raw(f"SELECT seq, record_hash FROM {TABLE} ORDER BY seq") == before
