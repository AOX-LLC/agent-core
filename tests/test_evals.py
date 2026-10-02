import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from aox_agent_core import AgentClient, Tier
from aox_agent_core.errors import EvalError
from aox_agent_core.evals import (
    EvalCase,
    EvalRunner,
    EvalSuite,
    ExactMatch,
    FieldMatch,
    TargetOutput,
    model_call_target,
    render_scorecard_markdown,
    write_scorecard_json,
)
from support import ScriptedProvider, make_config, response


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
