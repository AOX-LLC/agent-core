"""aox-agent-core: routed Claude calls with structured outputs, tracing with cost,
record/replay, human approvals, an append-only audit log and evals.

Subpackages: models, replay, tracing, approvals, audit, evals, testing.
"""

from importlib.metadata import version

from aox_agent_core.config import (
    AgentCoreConfig,
    BudgetAction,
    Effort,
    Mode,
    Provider,
    Tier,
    load_config,
)
from aox_agent_core.context import RunContext
from aox_agent_core.credentials import resolve_api_key
from aox_agent_core.errors import AgentCoreError
from aox_agent_core.models import (
    AgentClient,
    Attachment,
    CallResult,
    Message,
    ModelClient,
    PromptRef,
    Role,
    Usage,
)

__version__ = version("aox-agent-core")

__all__ = [
    "AgentClient",
    "AgentCoreConfig",
    "AgentCoreError",
    "Attachment",
    "BudgetAction",
    "CallResult",
    "Effort",
    "Message",
    "Mode",
    "ModelClient",
    "PromptRef",
    "Provider",
    "Role",
    "RunContext",
    "Tier",
    "Usage",
    "__version__",
    "load_config",
    "resolve_api_key",
]
