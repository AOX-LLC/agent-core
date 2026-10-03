"""What verify_report returns: every problem found in a walk of the chain, not the first."""

from typing import Annotated, Final, Literal

from pydantic import Field

from aox_agent_core._model import FrozenModel
from aox_agent_core.audit.types import AuditHead

# Most problems one report lists; the walk stops there and says so (`truncated`).
MAX_REPORTED_PROBLEMS: Final = 1000

ProblemKind = Literal["malformed", "seq", "link", "hash", "anchor"]


class VerifyProblem(FrozenModel):
    """One thing wrong with the chain, and where.

    kind says what: `malformed` (the row cannot be read as a record), `seq` (records are
    missing or repeated before this one), `link` (its prev_hash is not the previous
    record's hash), `hash` (its stored hash is not the hash of its fields: it was altered
    or cannot be hashed) or `anchor` (the log no longer matches the head kept elsewhere).
    `seq` is None only for an `anchor` problem about a log cut short.
    """

    seq: int | None
    kind: ProblemKind
    detail: str


class VerifyReport(FrozenModel):
    """The result of walking the whole chain without stopping at the first problem.

    `head` is the last record the walk could read (seq 0, the genesis hash, for an empty
    or wholly unreadable log), `records_checked` how many rows it looked at, and
    `problems` what it found, in seq order. After a bad record the walk goes on from that
    record's stored hash, so one altered record is reported once, at its own seq, and a
    rewritten run of records shows as one `link` problem where it joins the rest. `ok` is
    true only when there is no problem. An empty `problems` is no proof against someone
    who can rebuild every hash: pass `expected_head`, kept where the log's writers cannot
    reach, as verify() takes it.
    """

    head: AuditHead
    records_checked: Annotated[int, Field(ge=0)]
    problems: tuple[VerifyProblem, ...] = ()
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems
