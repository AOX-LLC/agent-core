"""The aox-agent-core command line: `cassettes check <dir>` and `audit verify [url]`."""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from aox_agent_core.audit.sql import SQLAuditLog, audit_table_exists
from aox_agent_core.audit.types import AuditHead
from aox_agent_core.config import AUDIT_DATABASE_URL_ENV, load_config
from aox_agent_core.errors import AgentCoreError, AuditIntegrityError, CassetteFormatError
from aox_agent_core.replay.keys import request_hash
from aox_agent_core.replay.recording import Recording
from aox_agent_core.replay.scrub import PatternScrubber
from aox_agent_core.replay.store import DirectoryRecordingStore, parse_recording, recorded_content
from aox_agent_core.storage import SQLiteDatabase, driver_errors, open_database

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
    verify.add_argument(
        "--schema", help="the Postgres schema the audit log was installed in (default: public)"
    )
    arguments = parser.parse_args(argv)

    if arguments.command == "audit":
        return _verify_audit(
            arguments.url, arguments.anchor_seq, arguments.anchor_hash, arguments.schema
        )
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


def _verify_audit(
    url: str | None, anchor_seq: int | None, anchor_hash: str | None, schema: str | None = None
) -> int:
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
        OSError,
        *driver_errors(),
    )
    try:
        database = open_database(database_url)
        if isinstance(database, SQLiteDatabase) and not database.path.is_file():
            print(f"error: no audit database at {database.path}", file=sys.stderr)
            return 2
        if not audit_table_exists(database, schema=schema):
            print("error: this database has no audit log table", file=sys.stderr)
            return 2
        log = SQLAuditLog(database, schema=schema)
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
    """Return one line per problem found in the recordings under `directory`.

    An empty list means every recording is valid and sits where its key says it
    belongs. Raises AgentCoreError if the configuration cannot be loaded.
    """
    paths = sorted(path for path in directory.rglob("*.json") if not path.name.startswith("."))
    if not paths:
        return [f"{directory}: no recordings (*.json) found"]

    scrubber = PatternScrubber(extra_patterns=load_config().replay.extra_secret_patterns)
    problems: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"{path}: unreadable ({type(error).__name__})")
            continue
        try:
            recording = parse_recording(text, source=path)
        except CassetteFormatError as error:
            problems.append(f"{path}: {_why_unreadable(error, path)}")
            continue
        problems += _problems_in(directory, path, recording, scrubber)
    return problems


def _problems_in(
    root: Path, path: Path, recording: Recording, scrubber: PatternScrubber
) -> list[str]:
    problems: list[str] = []
    # A redacted recording keeps the key of what was sent, which can no longer be
    # recomputed from what was written.
    is_redacted = REDACTION_MARKER in recording.model_dump_json()
    expected_key = (
        recording.prompt.key if recording.prompt is not None else request_hash(recording.request)
    )
    if not is_redacted and recording.replay_hash != expected_key:
        problems.append(f"{path}: key does not match its recorded call")

    store = DirectoryRecordingStore(root, scrubber=scrubber)
    cassette = path.parent.name if recording.prompt is None else "default"
    expected_path = store.path_of(recording, cassette=cassette)
    if path != expected_path:
        problems.append(f"{path}: misplaced; this recording belongs at {expected_path}")

    for finding in scrubber.find_secrets(recorded_content(recording.model_dump(mode="json"))):
        problems.append(f"{path}: possible secret ({finding.rule}) at {finding.path}")
    return problems


def _why_unreadable(error: CassetteFormatError, path: Path) -> str:
    cause = error.__cause__
    if not isinstance(cause, ValidationError):
        # Format 1, or not JSON: the error already says what is wrong.
        return str(error).removeprefix(f"{path} ")
    detail = cause.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in detail["loc"]) or "<root>"
    return f"not a valid recording ({location}: {detail['msg']})"


if __name__ == "__main__":
    sys.exit(main())
