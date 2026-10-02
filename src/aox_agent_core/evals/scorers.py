"""Built-in scorers."""

from collections.abc import Sequence

from pydantic import JsonValue

from aox_agent_core.evals.types import EvalCase, Score


class ExactMatch:
    """Passes when the output equals the case's expected value exactly."""

    name = "exact_match"

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        passed = output == case.expected
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed)


class FieldMatch:
    """Compares named fields of a structured output with the expected object.

    The value is the share of fields that match; the case passes only when all
    of them do. A missing field counts as a mismatch.
    """

    def __init__(self, fields: Sequence[str], *, name: str = "field_match") -> None:
        if not fields:
            raise ValueError("FieldMatch needs at least one field")
        self.fields = tuple(fields)
        self.name = name

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        if not isinstance(output, dict) or not isinstance(case.expected, dict):
            return Score(
                scorer=self.name,
                value=0.0,
                passed=False,
                detail="output or expected is not an object",
            )
        expected = case.expected
        mismatched = [
            field
            for field in self.fields
            if field not in output or field not in expected or output[field] != expected[field]
        ]
        matched = len(self.fields) - len(mismatched)
        return Score(
            scorer=self.name,
            value=matched / len(self.fields),
            passed=not mismatched,
            detail=f"mismatched: {', '.join(mismatched)}" if mismatched else None,
        )
