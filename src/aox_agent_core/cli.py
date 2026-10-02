"""The aox-agent-core command line: `cassettes check <dir>` and `audit verify [url]`."""

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from aox_agent_core.audit.sql import SQLAuditLog
from aox_agent_core.audit.types import AuditHead
from aox_agent_core.config import AUDIT_DATABASE_URL_ENV, load_config
from aox_agent_core.errors import AgentCoreError, AuditIntegrityError, CassetteFormatError
from aox_agent_core.replay.cassette import Cassette, request_hash
from aox_agent_core.replay.scrub import PatternScrubber
from aox_agent_core.replay.store import parse_cassette, recorded_content
from aox_agent_core.storage import driver_errors, open_database

REDACTION_MARKER = "[REDACTED:"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit code: 0 clean, 1 problems, 2 error."""
    parser = argparse.ArgumentParser(prog="aox-agent-core")
    commands = parser.add_subparsers(dest="command", required=True)

    cassettes = commands.add_parser("cassettes", help="work with replay cassettes")
    cassette_commands = cassettes.add_subparsers(dest="cassette_command", required=True)
    check = cassette_commands.add_parser(
        "check",
        help="validate cassettes and scan them for secrets",
        description=(
            "Check every *.json cassette in a directory: format, request hashes, "
            "sequence numbers, and secrets in recorded content. Exits 1 if anything "
            "is wrong and 2 if the check could not run. Secret patterns added in "
            "AGENT_CORE_CONFIG are applied too."
        ),
    )
    check.add_argument("directory", type=Path)

    audit = commands.add_parser("audit", help="work with the audit log")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    verify = audit_commands.add_parser(
        "verify",
        help="check the audit chain",
        description=(
            "Walk the audit log's hash chain and print its head. Pass the head you "
            "saved earlier with --anchor-seq and --anchor-hash: without an anchor the "
            "chain cannot show that it was not rewritten or cut short. Exits 1 if the "
            "check fails. The URL is read from AGENT_CORE_AUDIT_DATABASE_URL if omitted."
        ),
    )
    verify.add_argument("url", nargs="?")
    verify.add_argument("--anchor-seq", type=int)
    verify.add_argument("--anchor-hash")
    arguments = parser.parse_args(argv)

    if arguments.command == "audit":
        return _verify_audit(arguments.url, arguments.anchor_seq, arguments.anchor_hash)
    return _check_cassettes_command(arguments.directory)


def _check_cassettes_command(directory: Path) -> int:
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 2
    try:
        problems = check_cassettes(directory)
    except AgentCoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s) found.", file=sys.stderr)
        return 1
    print(f"All cassettes in {directory} are valid.")
    return 0


def _verify_audit(url: str | None, anchor_seq: int | None, anchor_hash: str | None) -> int:
    if (anchor_seq is None) != (anchor_hash is None):
        print("error: pass --anchor-seq and --anchor-hash together", file=sys.stderr)
        return 2
    if url is not None and urlsplit(url).password is not None:
        print(
            f"warning: the URL includes a password, which shell history and process lists "
            f"can show; prefer {AUDIT_DATABASE_URL_ENV}",
            file=sys.stderr,
        )
    database_url = url or os.environ.get(AUDIT_DATABASE_URL_ENV, "").strip()
    if not database_url:
        print(f"error: pass a database URL or set {AUDIT_DATABASE_URL_ENV}", file=sys.stderr)
        return 2
    reportable_errors: tuple[type[Exception], ...] = (
        AgentCoreError,
        ValidationError,
        *driver_errors(),
    )
    try:
        log = SQLAuditLog(open_database(database_url))
        anchor = (
            AuditHead(seq=anchor_seq, record_hash=anchor_hash)
            if anchor_seq is not None and anchor_hash is not None
            else None
        )
        head = asyncio.run(log.verify(expected_head=anchor))
    except AuditIntegrityError as error:
        print(f"FAILED: {error}")
        return 1
    except reportable_errors as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    anchored = " and matches the anchor" if anchor is not None else " (no anchor given)"
    print(f"OK: {head.seq} records, chain intact{anchored}. Head: {head.seq} {head.record_hash}")
    return 0


def check_cassettes(directory: Path) -> list[str]:
    """Return one line per problem found in the cassettes under `directory`.

    An empty list means every cassette is valid. Raises AgentCoreError if the
    configuration cannot be loaded.
    """
    paths = sorted(directory.glob("*.json"))
    if not paths:
        return [f"{directory}: no cassettes (*.json) found"]

    scrubber = PatternScrubber(extra_patterns=load_config().replay.extra_secret_patterns)
    problems: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"{path}: unreadable ({type(error).__name__})")
            continue
        try:
            cassette = parse_cassette(text, source=path)
        except CassetteFormatError as error:
            problems.append(f"{path}: not a valid cassette ({_first_validation_problem(error)})")
            continue
        problems += _problems_in(path, cassette, scrubber)
    return problems


def _problems_in(path: Path, cassette: Cassette, scrubber: PatternScrubber) -> list[str]:
    problems: list[str] = []
    if cassette.name != path.stem:
        problems.append(f"{path}: name {cassette.name!r} does not match the file name")

    sequences: dict[str, list[int]] = defaultdict(list)
    for index, entry in enumerate(cassette.entries):
        # A redacted entry keeps the hash of the request as sent, which can no
        # longer be recomputed from what was written.
        is_redacted = REDACTION_MARKER in entry.request.model_dump_json()
        if not is_redacted and entry.request_hash != request_hash(entry.request):
            problems.append(f"{path}: entries[{index}] request_hash does not match its request")
        sequences[entry.request_hash].append(entry.sequence)
    for key, numbers in sequences.items():
        if sorted(numbers) != list(range(len(numbers))):
            problems.append(f"{path}: request {key[:12]} has sequence numbers {sorted(numbers)}")

    for finding in scrubber.find_secrets(recorded_content(cassette.model_dump(mode="json"))):
        problems.append(f"{path}: possible secret ({finding.rule}) at {finding.path}")
    return problems


def _first_validation_problem(error: CassetteFormatError) -> str:
    cause = error.__cause__
    if not isinstance(cause, ValidationError):
        return "unreadable"
    detail = cause.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in detail["loc"]) or "<root>"
    return f"{location}: {detail['msg']}"


if __name__ == "__main__":
    sys.exit(main())
