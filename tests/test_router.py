from decimal import Decimal

import pytest

from aox_agent_core import Tier
from aox_agent_core.errors import BudgetExceededError, ConfigError
from aox_agent_core.models import ConfigRouter, RouteDecision, RouteRequest
from support import make_config

# Default prices per million tokens: small 1/5, mid 2/10, large 4/20 (input/output).


def route(budget: str | None = None, action: str = "raise", **request: object) -> RouteDecision:
    config = make_config(
        routing={
            "tasks": {"extraction": "small", "synthesis": "large"},
            "budget_usd_per_call": budget,
            "on_budget_exceeded": action,
        }
    )
    return ConfigRouter(config).select(RouteRequest.model_validate(request))


def test_explicit_tier_is_used() -> None:
    decision = route(tier="large", estimated_input_tokens=10)

    assert decision.tier is Tier.LARGE
    assert decision.model == "claude-opus-5-5"
    assert decision.reason == "tier large requested"


def test_task_maps_to_its_tier() -> None:
    decision = route(task="extraction", estimated_input_tokens=10)

    assert decision.tier is Tier.SMALL
    assert decision.reason == "task extraction maps to small"


def test_unknown_task_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="planning"):
        route(task="planning", estimated_input_tokens=10)


def test_default_tier_applies_when_nothing_is_named() -> None:
    assert route(estimated_input_tokens=10).tier is Tier.MID


def test_max_tokens_defaults_to_the_tier_setting() -> None:
    assert route(tier="small", estimated_input_tokens=10).max_tokens == 16_000
    assert route(tier="small", estimated_input_tokens=10, max_output_tokens=500).max_tokens == 500


def test_estimate_is_worst_case_input_plus_max_output() -> None:
    decision = route(tier="mid", estimated_input_tokens=1_000, max_output_tokens=1_000)

    assert decision.estimated_cost_usd == Decimal("0.012")


def test_call_within_budget_keeps_its_tier() -> None:
    decision = route("0.05", tier="large", estimated_input_tokens=1_000, max_output_tokens=1_000)

    assert decision.tier is Tier.LARGE


def test_over_budget_raises_by_default() -> None:
    with pytest.raises(BudgetExceededError, match=r"\$0\.024 on tier large"):
        route("0.02", tier="large", estimated_input_tokens=1_000, max_output_tokens=1_000)


def test_over_budget_can_drop_one_tier() -> None:
    decision = route(
        "0.02", "drop_one_tier", tier="large", estimated_input_tokens=1_000, max_output_tokens=1_000
    )

    assert (decision.requested_tier, decision.tier) == (Tier.LARGE, Tier.MID)
    assert decision.model == "claude-sonnet-5-5"
    assert "dropped to mid" in decision.reason


def test_drops_only_one_tier() -> None:
    with pytest.raises(BudgetExceededError, match="tier mid"):
        route(
            "0.01",
            "drop_one_tier",
            tier="large",
            estimated_input_tokens=1_000,
            max_output_tokens=1_000,
        )


def test_small_tier_has_nowhere_to_drop() -> None:
    with pytest.raises(BudgetExceededError, match="tier small"):
        route(
            "0.001",
            "drop_one_tier",
            tier="small",
            estimated_input_tokens=1_000,
            max_output_tokens=1_000,
        )
