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
from aox_agent_core.replay import DirectoryCassetteStore, PatternScrubber, RecordingProvider
from aox_agent_core.storage import open_database
from support import ScriptedProvider, response

EXAMPLE_CASSETTES = Path(__file__).parents[1] / "examples" / "replays"


@pytest.fixture
def cassettes(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLE_CASSETTES, tmp_path, dirs_exist_ok=True)
    return tmp_path


def edit(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = json.loads(path.read_text())
    change(document)
    path.write_text(json.dumps(document))


def test_example_cassettes_pass(cassettes: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cassettes", "check", str(cassettes)]) == 0
    assert "valid" in capsys.readouterr().out


def test_tampered_request_is_reported(cassettes: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def change_prompt(document: dict[str, Any]) -> None:
        document["entries"][0]["request"]["max_tokens"] += 1

    edit(cassettes / "routed-call.json", change_prompt)

    assert main(["cassettes", "check", str(cassettes)]) == 1
    assert "request_hash does not match" in capsys.readouterr().out


def test_secret_is_reported_without_its_value(
    cassettes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_key = "sk-ant-" + "z" * 24

    def plant_secret(document: dict[str, Any]) -> None:
        document["entries"][0]["response"]["text"] = f"key {fake_key}"

    edit(cassettes / "routed-call.json", plant_secret)

    assert main(["cassettes", "check", str(cassettes)]) == 1
    output = capsys.readouterr().out
    assert "possible secret (anthropic_api_key)" in output
    assert fake_key not in output


def test_gap_in_sequence_numbers_is_reported(
    cassettes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def skip_sequence(document: dict[str, Any]) -> None:
        document["entries"][0]["sequence"] = 1

    edit(cassettes / "routed-call.json", skip_sequence)

    assert main(["cassettes", "check", str(cassettes)]) == 1
    assert "sequence numbers [1]" in capsys.readouterr().out


def test_invalid_file_and_empty_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cassettes", "check", str(tmp_path)]) == 1
    (tmp_path / "broken.json").write_text("{}")

    assert main(["cassettes", "check", str(tmp_path)]) == 1
    assert "not a valid cassette (name: Field required)" in capsys.readouterr().out


async def test_redacted_cassette_passes_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_key = "sk-ant-" + "r" * 24
    redacting = DirectoryCassetteStore(
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
    assert fake_key not in (tmp_path / "redacted.json").read_text()


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
