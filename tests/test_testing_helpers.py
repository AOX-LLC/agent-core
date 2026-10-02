from pathlib import Path

import pytest

from aox_agent_core.testing import cassette_client
from aox_agent_core.testing.pytest_plugin import _cassette_name

pytest_plugins = ["pytester"]


def test_cassette_client_binds_the_named_cassette() -> None:
    client = cassette_client("triage-urgent")

    assert client.config.replay.cassette == "triage-urgent"


def test_cassette_client_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError, match="cassette"):
        cassette_client("../escape")


@pytest.mark.parametrize(
    ("test_id", "name"),
    [
        ("test_triage-test_route[urgent-2]", "test_triage-test_route_urgent-2"),
        ("test_a-test_b[x/y]", "test_a-test_b_x_y"),
    ],
)
def test_test_ids_become_valid_cassette_names(test_id: str, name: str) -> None:
    assert _cassette_name(test_id) == name


def test_use_cassette_fixture_replays_in_a_consumer_project(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    examples = Path(__file__).parents[1] / "examples"
    monkeypatch.setenv("AGENT_CORE_CONFIG", str(examples / "agent-core.toml"))
    pytester.makeconftest('pytest_plugins = ["aox_agent_core.testing.pytest_plugin"]')
    pytester.makepyfile(
        f"""
        import sys

        import pytest
        from aox_agent_core.errors import ReplayMissError

        sys.path.insert(0, {str(examples)!r})
        from routed_call import triage

        def test_recorded_call_replays(use_cassette):
            assert triage(use_cassette("routed-call")).output.queue == "billing"

        def test_unrecorded_prompt_misses(use_cassette):
            with pytest.raises(ReplayMissError):
                use_cassette("routed-call").call_sync("a prompt nobody recorded")
        """
    )

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=2)
