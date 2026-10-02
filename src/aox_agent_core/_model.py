"""Shared building blocks for the library's Pydantic models."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# Opaque identifiers for people, services and the things they act on. '@' is
# excluded so an email address can never be used as an identifier and end up in
# an append-only audit record, where it could not be erased.
PrincipalId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
SubjectId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")]

# Letters, digits, '-' and '_' only, so a cassette name can never escape the
# cassette directory.
CassetteName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]

# Dotted lowercase names such as "model.call" or "crm.update_contact".
ActionName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", max_length=100),
]


class FrozenModel(BaseModel):
    """Immutable model that rejects unknown fields.

    Validation errors never echo the rejected input: a guard that refuses a
    secret must not then print it into a log.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
