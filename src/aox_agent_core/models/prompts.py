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
        """Fill the template. Strings go in as they are, other values as compact JSON.

        Raises PromptError naming a placeholder that has no input.
        """
        values = {
            name: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for name, value in inputs.items()
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
