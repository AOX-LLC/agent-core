"""Choosing a tier and model for each call."""

from decimal import Decimal
from typing import Annotated, Protocol, Self

from pydantic import Field, model_validator

from aox_agent_core._model import FrozenModel
from aox_agent_core.config import AgentCoreConfig, BudgetAction, Provider, Tier
from aox_agent_core.errors import BudgetExceededError
from aox_agent_core.models.pricing import worst_case_cost


class RouteRequest(FrozenModel):
    """What the router needs to know about a call. Name a tier or a task, not both.

    With neither, the router uses routing.default_tier. max_output_tokens left at
    None means the chosen tier's configured max_tokens.
    """

    tier: Tier | None = None
    task: str | None = None
    estimated_input_tokens: Annotated[int, Field(ge=0)]
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None

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
    max_tokens: Annotated[int, Field(gt=0)]
    estimated_cost_usd: Annotated[Decimal, Field(ge=0)] | None
    reason: str


class Router(Protocol):
    """Picks the tier and model for a call."""

    def select(self, request: RouteRequest) -> RouteDecision: ...


class ConfigRouter:
    """The default router, driven entirely by AgentCoreConfig.routing.

    Resolves a task to a tier through routing.tasks, estimates the worst-case cost
    (input plus max output tokens at the tier's prices), and applies
    routing.on_budget_exceeded when that is over routing.budget_usd_per_call:
    RAISE raises BudgetExceededError; DROP_ONE_TIER tries the next cheaper tier
    once and raises only if that is still over budget or there is none.
    """

    def __init__(self, config: AgentCoreConfig) -> None:
        self._config = config

    def select(self, request: RouteRequest) -> RouteDecision:
        """Return the route for `request`, or raise BudgetExceededError if none fits."""
        requested_tier, basis = self._requested_tier(request)
        decision = self._decide(request, requested_tier, requested_tier, basis)
        if self._is_within_budget(decision):
            return decision

        lower_tier = requested_tier.one_lower()
        can_drop = self._config.routing.on_budget_exceeded is BudgetAction.DROP_ONE_TIER
        if can_drop and lower_tier is not None:
            dropped = self._decide(
                request,
                requested_tier,
                lower_tier,
                f"{basis}; dropped to {lower_tier.value}: {requested_tier.value} was over budget",
            )
            if self._is_within_budget(dropped):
                return dropped
            decision = dropped

        raise BudgetExceededError(
            f"Worst-case cost ${decision.estimated_cost_usd} on tier {decision.tier.value} is "
            f"over the per-call budget ${self._config.routing.budget_usd_per_call}."
        )

    def _requested_tier(self, request: RouteRequest) -> tuple[Tier, str]:
        routing = self._config.routing
        if request.tier is not None:
            return request.tier, f"tier {request.tier.value} requested"
        if request.task is not None:
            tier = routing.tier_for_task(request.task)
            return tier, f"task {request.task} maps to {tier.value}"
        return routing.default_tier, f"default tier {routing.default_tier.value}"

    def _decide(
        self, request: RouteRequest, requested_tier: Tier, tier: Tier, reason: str
    ) -> RouteDecision:
        tier_config = self._config.routing.tiers[tier]
        max_tokens = request.max_output_tokens or tier_config.max_tokens
        price = self._config.price_for(tier_config.provider, tier_config.model)
        return RouteDecision(
            requested_tier=requested_tier,
            tier=tier,
            provider=tier_config.provider,
            model=tier_config.model,
            max_tokens=max_tokens,
            estimated_cost_usd=worst_case_cost(request.estimated_input_tokens, max_tokens, price),
            reason=reason,
        )

    def _is_within_budget(self, decision: RouteDecision) -> bool:
        budget = self._config.routing.budget_usd_per_call
        return (
            budget is None
            or decision.estimated_cost_usd is None
            or decision.estimated_cost_usd <= budget
        )
