import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aox_agent_core.cli import main

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
