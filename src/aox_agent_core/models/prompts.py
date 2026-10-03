"""Versioned prompt templates, rendered by the library."""

import json
from collections.abc import Mapping
from string import Template
from typing import Annotated

from pydantic import Field, JsonValue, StringConstraints

from aox_agent_core._model import FrozenModel
from aox_agent_core.errors import PromptError

PromptId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]


class PromptRef(FrozenModel):
    """A prompt template with an identity, e.g. PromptRef(id="receipts.extract", version=3, ...).

    The template uses string.Template syntax (${name}). The id and version, not
    the template text, identify a prompt in replay keys: change the template or
    the system prompt and bump the version, or replay raises
    StaleRecordingError for recordings made with the old text.
    """

    id: PromptId
    version: Annotated[int, Field(ge=1)]
    template: Annotated[str, Field(min_length=1)]
    system: str | None = None

    def render(self, inputs: Mapping[str, JsonValue]) -> str:
        """Fill the template. Strings go in as they are, other values as compact JSON
        with sorted keys, so inputs that share a replay key render the same text.

        Raises PromptError naming a placeholder that has no input, or if an input
        is not plain JSON.
        """
        values = {
            name: value if isinstance(value, str) else _compact_json(value)
            for name, value in checked_inputs(inputs).items()
        }
        try:
            return Template(self.template).substitute(values)
        except KeyError as error:
            raise PromptError(
                f"Prompt {self.id} v{self.version} needs input {error.args[0]!r}."
            ) from None
        except ValueError as error:
            raise PromptError(
                f"Prompt {self.id} v{self.version} has a bad placeholder: {error}"
            ) from error


def checked_inputs(inputs: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The inputs as plain JSON values: tuples become lists, and so on.

    Raises PromptError if a value is not JSON (a Decimal, a datetime) or is a
    float that JSON cannot hold (NaN, infinity).
    """
    try:
        loaded: dict[str, JsonValue] = json.loads(_compact_json(dict(inputs)))
    except (TypeError, ValueError) as error:
        raise PromptError(f"Prompt inputs must be plain JSON values: {error}") from None
    return loaded


def _compact_json(value: JsonValue) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
