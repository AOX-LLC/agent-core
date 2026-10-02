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
from aox_agent_core.credentials import resolve_api_key
from aox_agent_core.errors import AgentCoreError
from aox_agent_core.models import AgentClient, CallResult, Message, Role, Usage

__version__ = version("aox-agent-core")

__all__ = [
    "AgentClient",
    "AgentCoreConfig",
    "AgentCoreError",
    "BudgetAction",
    "CallResult",
    "Effort",
    "Message",
    "Mode",
    "Provider",
    "Role",
    "Tier",
    "Usage",
    "__version__",
    "load_config",
    "resolve_api_key",
]
