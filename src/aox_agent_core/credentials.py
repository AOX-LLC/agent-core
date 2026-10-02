"""API key lookup.

The key comes from an explicit argument or from AGENT_CORE_ANTHROPIC_API_KEY, and
from nowhere else. The Anthropic SDK's own variable is never read, so a project
cannot silently spend on a key it inherited from the shell, such as one exported
for a developer tool.
"""

import os
from collections.abc import Mapping

from pydantic import SecretStr

from aox_agent_core.errors import MissingCredentialsError

API_KEY_ENV = "AGENT_CORE_ANTHROPIC_API_KEY"


def resolve_api_key(
    explicit: str | SecretStr | None = None, *, environ: Mapping[str, str] | None = None
) -> SecretStr:
    """Return the API key from `explicit`, else from AGENT_CORE_ANTHROPIC_API_KEY."""
    if explicit is not None:
        explicit_key = (
            explicit.get_secret_value() if isinstance(explicit, SecretStr) else explicit
        ).strip()
        if not explicit_key:
            raise MissingCredentialsError("The api_key argument is empty.")
        return SecretStr(explicit_key)

    env = os.environ if environ is None else environ
    environment_key = env.get(API_KEY_ENV, "").strip()
    if not environment_key:
        raise MissingCredentialsError(
            f"No Anthropic API key: pass api_key=... or set {API_KEY_ENV}. "
            "Replay mode needs no key."
        )
    return SecretStr(environment_key)
