import asyncio
import json
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from aox_agent_core import Message, Provider, Role
from aox_agent_core.audit import AuditEvent, SQLAuditLog
from aox_agent_core.cli import main
from aox_agent_core.config import SecretAction
from aox_agent_core.models import ProviderRequest
from aox_agent_core.replay import DirectoryRecordingStore, PatternScrubber, RecordingProvider
from aox_agent_core.storage import open_database
from support import ScriptedProvider, response

EXAMPLE_CASSETTES = Path(__file__).parents[1] / "examples" / "replays"


@pytest.fixture
def cassettes(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLE_CASSETTES, tmp_path, dirs_exist_ok=True)
    return tmp_path


def only_recording(directory: Path) -> Path:
    (path,) = directory.rglob("*.json")
    return path


def edit(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = json.loads(path.read_text())
    change(document)
    path.write_text(json.dumps(document))


def test_example_cassettes_pass(cassettes: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cassettes", "check", str(cassettes)]) == 0
    assert "valid" in capsys.readouterr().out


def test_tampered_request_is_reported(cassettes: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def change_prompt(document: dict[str, Any]) -> None:
        document["request"]["max_tokens"] += 1

    edit(only_recording(cassettes), change_prompt)

    assert main(["cassettes", "check", str(cassettes)]) == 1
    assert "key does not match its recorded call" in capsys.readouterr().out


def test_secret_is_reported_without_its_value(
    cassettes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_key = "sk-ant-" + "z" * 24

    def plant_secret(document: dict[str, Any]) -> None:
        document["response"]["text"] = f"key {fake_key}"

    edit(only_recording(cassettes), plant_secret)

    assert main(["cassettes", "check", str(cassettes)]) == 1
    output = capsys.readouterr().out
    assert "possible secret (anthropic_api_key)" in output
    assert fake_key not in output


def test_a_misplaced_recording_is_reported(
    cassettes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recording = only_recording(cassettes)
    recording.rename(recording.with_name("renamed.0.json"))

    assert main(["cassettes", "check", str(cassettes)]) == 1
    assert "misplaced; this recording belongs at" in capsys.readouterr().out


def test_a_format_1_cassette_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "old.json").write_text(json.dumps({"format_version": 1, "entries": []}))

    assert main(["cassettes", "check", str(tmp_path)]) == 1
    assert "is a format 1 cassette from agent-core 0.1.0a1" in capsys.readouterr().out


def test_invalid_file_and_empty_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cassettes", "check", str(tmp_path)]) == 1
    (tmp_path / "broken.json").write_text("{}")

    assert main(["cassettes", "check", str(tmp_path)]) == 1
    assert "not a valid recording (replay_hash: Field required)" in capsys.readouterr().out


async def test_redacted_cassette_passes_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_key = "sk-ant-" + "r" * 24
    redacting = DirectoryRecordingStore(
        tmp_path, scrubber=PatternScrubber(), on_secret=SecretAction.REDACT
    )
    recorder = RecordingProvider(ScriptedProvider(response("ok")), redacting, "redacted")
    await recorder.complete(
        ProviderRequest(
            provider=Provider.ANTHROPIC,
            model="claude-haiku-4-5-20251001",
            messages=(Message(role=Role.USER, content=f"key {fake_key}"),),
            max_tokens=10,
        )
    )

    assert main(["cassettes", "check", str(tmp_path)]) == 0
    assert fake_key not in only_recording(tmp_path).read_text()


def test_unreadable_file_is_a_problem_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00")

    assert main(["cassettes", "check", str(tmp_path)]) == 1
    assert "unreadable (UnicodeDecodeError)" in capsys.readouterr().out


def test_missing_directory_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cassettes", "check", str(tmp_path / "absent")]) == 2
    assert "is not a directory" in capsys.readouterr().err


async def seeded_audit_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'audit.sqlite3'}"
    log = SQLAuditLog(open_database(url))
    for number in range(3):
        await log.append(
            AuditEvent(action="model.call", actor_id="svc-1", subject_id=f"t-{number}")
        )
    return url


async def test_audit_verify_reports_an_intact_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = await seeded_audit_url(tmp_path)
    head = await SQLAuditLog(open_database(url)).head()

    exit_code = await asyncio.to_thread(
        main, ["audit", "verify", url, "--anchor-seq", "3", "--anchor-hash", head.record_hash]
    )

    assert exit_code == 0
    assert "OK: 3 records, chain intact and matches the anchor" in capsys.readouterr().out


async def test_audit_verify_fails_on_an_edited_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = await seeded_audit_url(tmp_path)
    path = url.removeprefix("sqlite:///")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER agent_core_audit_no_update")
        connection.execute("UPDATE agent_core_audit SET actor_id = 'svc-2' WHERE seq = 2")

    assert await asyncio.to_thread(main, ["audit", "verify", url]) == 1
    assert "FAILED: Record 2 was altered" in capsys.readouterr().out


def test_audit_verify_needs_a_url_and_a_whole_anchor(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audit", "verify"]) == 2
    assert main(["audit", "verify", "sqlite:///x.sqlite3", "--anchor-seq", "1"]) == 2
    assert "together" in capsys.readouterr().err


def test_audit_verify_warns_about_a_password_in_the_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["audit", "verify", "postgresql://app:hunter2@127.0.0.1:9/none"])

    errors = capsys.readouterr().err
    assert exit_code == 2  # nothing listens on port 9: reported, not a traceback
    assert "includes a password" in errors
    assert "OperationalError" in errors


def test_audit_verify_refuses_a_missing_sqlite_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "typo" / "audit.sqlite3"

    assert main(["audit", "verify", f"sqlite:///{missing}"]) == 2
    assert "no audit database" in capsys.readouterr().err
    assert not missing.parent.exists()


def test_audit_verify_refuses_a_database_without_an_audit_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as connection, connection:
        connection.execute("CREATE TABLE unrelated (a INTEGER)")

    assert main(["audit", "verify", f"sqlite:///{other}"]) == 2
    assert "no audit log table" in capsys.readouterr().err
