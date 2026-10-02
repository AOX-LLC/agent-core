import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from aox_agent_core import AgentClient, Mode, Tier
from aox_agent_core.errors import EvalError
from aox_agent_core.evals import (
    EvalCase,
    EvalRunner,
    EvalSuite,
    ExactMatch,
    FieldMatch,
    Scorecard,
    TargetOutput,
    model_call_target,
    render_scorecard_markdown,
    write_scorecard_json,
)
from support import ScriptedProvider, make_config, response

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def write_suite(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "sample.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_suite_loads_from_jsonl_skipping_blank_lines(tmp_path: Path) -> None:
    path = write_suite(
        tmp_path, '{"id": "a", "input": "1", "expected": "x"}', "", '{"id": "b", "input": "2"}'
    )

    suite = EvalSuite.from_jsonl(path)

    assert suite.name == "sample"
    assert [case.id for case in suite.cases] == ["a", "b"]


def test_invalid_line_names_its_line_number(tmp_path: Path) -> None:
    path = write_suite(tmp_path, '{"id": "a", "input": "1"}', '{"input": "no id"}')

    with pytest.raises(EvalError, match=r"sample\.jsonl:2"):
        EvalSuite.from_jsonl(path)


def test_empty_suite_is_an_eval_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="not a valid eval suite"):
        EvalSuite.from_jsonl(write_suite(tmp_path, ""))


def test_exact_match() -> None:
    case = EvalCase(id="a", input="q", expected={"x": 1})

    assert ExactMatch().score(case, {"x": 1}).passed
    assert not ExactMatch().score(case, {"x": 2}).passed


def test_field_match_scores_the_share_of_matching_fields() -> None:
    case = EvalCase(id="a", input="q", expected={"queue": "billing", "urgent": True})
    scorer = FieldMatch(["queue", "urgent"])

    full = scorer.score(case, {"queue": "billing", "urgent": True, "summary": "ignored"})
    half = scorer.score(case, {"queue": "billing", "urgent": False})
    missing = scorer.score(case, {"queue": "billing"})
    not_an_object = scorer.score(case, "billing")

    assert (full.value, full.passed) == (1.0, True)
    assert (half.value, half.passed, half.detail) == (0.5, False, "mismatched: urgent")
    assert missing.detail == "mismatched: urgent"
    assert not not_an_object.passed


async def test_runner_reports_accuracy_failures_latency_and_cost() -> None:
    suite = EvalSuite(
        name="sample",
        cases=(
            EvalCase(id="right", input="1", expected="one"),
            EvalCase(id="wrong", input="2", expected="two"),
            EvalCase(id="crash", input="3", expected="three"),
            EvalCase(id="right-2", input="4", expected="four"),
        ),
    )
    answers = {"1": "one", "2": "TWO", "4": "four"}

    async def target(case: EvalCase) -> TargetOutput:
        if case.id == "crash":
            raise RuntimeError("provider timed out")
        assert isinstance(case.input, str)
        return TargetOutput(output=answers[case.input], cost_usd=Decimal("0.001"))

    scorecard = await EvalRunner([ExactMatch()], concurrency=2).run(suite, target)

    assert scorecard.accuracy == 0.5
    assert [result.case_id for result in scorecard.results] == [
        "right",
        "wrong",
        "crash",
        "right-2",
    ]
    assert [result.case_id for result in scorecard.failures()] == ["wrong", "crash"]
    assert scorecard.results[2].error == "RuntimeError: provider timed out"
    assert scorecard.cost_total_usd == Decimal("0.003")
    assert scorecard.cost_per_case_usd == Decimal("0.00075")
    assert scorecard.mode is None
    assert scorecard.latency_p50_ms is not None
    assert scorecard.latency_p95_ms is not None
    assert 0 <= scorecard.latency_p50_ms <= scorecard.latency_p95_ms


class Triage(BaseModel):
    queue: str
    urgent: bool


async def test_model_call_target_returns_json_output_and_cost() -> None:
    provider = ScriptedProvider(
        response('{"queue": "billing", "urgent": true}', input_tokens=1_000, output_tokens=200)
    )
    client = AgentClient(make_config(), provider=provider)
    target = model_call_target(client, output=Triage, tier=Tier.SMALL)

    produced = await target(EvalCase(id="a", input="charged twice"))

    assert produced.output == {"queue": "billing", "urgent": True}
    assert produced.cost_usd == Decimal("0.002")


async def test_scorecard_writers(tmp_path: Path) -> None:
    suite = EvalSuite(
        name="sample",
        cases=(
            EvalCase(id="ok", input="1", expected="a"),
            EvalCase(id="bad", input="2", expected="b"),
        ),
    )

    async def target(case: EvalCase) -> TargetOutput:
        return TargetOutput(output="a|pipe" if case.id == "bad" else "a")

    scorecard = await EvalRunner([ExactMatch()]).run(suite, target)
    write_scorecard_json(scorecard, tmp_path / "scorecard.json")
    markdown = render_scorecard_markdown(scorecard)

    document = json.loads((tmp_path / "scorecard.json").read_text())
    assert document["format_version"] == 1
    assert document["accuracy"] == 0.5
    assert "| 2 | 1 | 50.0% |" in markdown
    assert "| bad | exact_match: failed |" in markdown


async def test_target_errors_are_scrubbed_in_the_scorecard() -> None:
    suite = EvalSuite(name="sample", cases=(EvalCase(id="leaky", input="1"),))
    fake_key = "sk-ant-" + "e" * 24

    async def target(case: EvalCase) -> TargetOutput:
        raise RuntimeError(f"request failed with key {fake_key}")

    scorecard = await EvalRunner([ExactMatch()]).run(suite, target)

    assert scorecard.results[0].error == (
        "RuntimeError: request failed with key [REDACTED:anthropic_api_key]"
    )


def two_case_suite() -> EvalSuite:
    return EvalSuite(
        name="sample",
        cases=(
            EvalCase(id="ok", input="1", expected="a"),
            EvalCase(id="bad", input="2", expected="b"),
        ),
    )


async def answer_a(case: EvalCase) -> TargetOutput:
    return TargetOutput(output="a")


async def test_replayed_run_reports_its_mode_and_no_latency(tmp_path: Path) -> None:
    scorecard = await EvalRunner([ExactMatch()]).run(two_case_suite(), answer_a, mode=Mode.REPLAY)
    write_scorecard_json(scorecard, tmp_path / "scorecard.json")
    markdown = render_scorecard_markdown(scorecard)

    document = json.loads((tmp_path / "scorecard.json").read_text())
    assert document["mode"] == "replay"
    assert document["latency_p50_ms"] is None
    assert document["latency_p95_ms"] is None
    assert all(result["latency_ms"] is None for result in document["results"])
    assert "Mode: replay. Responses came from recordings, so no latency is reported" in markdown
    assert "| 2 | 1 | 50.0% | n/a | n/a |" in markdown


async def test_live_run_reports_its_mode_and_real_latency() -> None:
    scorecard = await EvalRunner([ExactMatch()]).run(two_case_suite(), answer_a, mode=Mode.LIVE)
    markdown = render_scorecard_markdown(scorecard)

    assert scorecard.mode is Mode.LIVE
    assert scorecard.latency_p50_ms is not None
    assert "Mode: live." in markdown
    assert " ms |" in markdown


def test_a_replayed_scorecard_cannot_carry_latency() -> None:
    with pytest.raises(ValidationError, match="must not report latency"):
        Scorecard(
            suite="sample",
            mode=Mode.REPLAY,
            started_at=NOW,
            finished_at=NOW,
            results=(),
            accuracy=0,
            latency_p50_ms=1.0,
            latency_p95_ms=None,
            cost_total_usd=Decimal(0),
            cost_per_case_usd=Decimal(0),
        )


async def test_failures_render_with_their_reasons() -> None:
    """Failure rendering is covered here, independent of how the live suite scores."""
    suite = EvalSuite(
        name="sample",
        cases=(
            EvalCase(id="right", input="1", expected={"queue": "billing", "urgent": True}),
            EvalCase(id="wrong-field", input="2", expected={"queue": "billing", "urgent": True}),
            EvalCase(id="crashed", input="3", expected={"queue": "billing", "urgent": True}),
        ),
    )

    async def target(case: EvalCase) -> TargetOutput:
        if case.id == "crashed":
            raise RuntimeError("upstream | timeout")
        urgent = case.id == "right"
        return TargetOutput(output={"queue": "billing", "urgent": urgent})

    scorecard = await EvalRunner([FieldMatch(["queue", "urgent"])]).run(suite, target)
    markdown = render_scorecard_markdown(scorecard)

    assert "### Failed cases (2)" in markdown
    assert "| wrong-field | field_match: mismatched: urgent |" in markdown
    assert "| crashed | RuntimeError: upstream \\| timeout |" in markdown
    assert "| right |" not in markdown
