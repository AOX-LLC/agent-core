"""Choosing a tier and model for each call."""

from decimal import Decimal
from typing import Annotated, Protocol, Self

from pydantic import Field, model_validator

from aox_agent_core._model import FrozenModel
from aox_agent_core.config import AgentCoreConfig, Provider, Tier


class RouteRequest(FrozenModel):
    """What the router needs to know about a call. Name a tier or a task, not both.

    With neither, the router uses routing.default_tier.
    """

    tier: Tier | None = None
    task: str | None = None
    estimated_input_tokens: Annotated[int, Field(ge=0)]
    max_output_tokens: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _tier_or_task_not_both(self) -> Self:
        if self.tier is not None and self.task is not None:
            raise ValueError("pass tier or task, not both")
        return self


class RouteDecision(FrozenModel):
    """Where a call goes, and why.

    requested_tier differs from tier when the router dropped a tier to stay
    within budget.
    """

    requested_tier: Tier
    tier: Tier
    provider: Provider
    model: str
    estimated_cost_usd: Annotated[Decimal, Field(ge=0)] | None
    reason: str


class Router(Protocol):
    """Picks the tier and model for a call."""

    def select(self, request: RouteRequest) -> RouteDecision: ...


class ConfigRouter:
    """The default router, driven entirely by AgentCoreConfig.routing.

    Resolves a task to a tier through routing.tasks, estimates the worst-case cost
    (input plus max_output_tokens at the tier's prices), and applies
    routing.on_budget_exceeded when that is over routing.budget_usd_per_call.
    """

    def __init__(self, config: AgentCoreConfig) -> None:
        self._config = config

    def select(self, request: RouteRequest) -> RouteDecision:
        raise NotImplementedError("ConfigRouter.select is not implemented yet.")
