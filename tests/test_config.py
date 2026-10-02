from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from aox_agent_core.config import (
    AgentCoreConfig,
    BudgetAction,
    Mode,
    Provider,
    ReplayConfig,
    Tier,
    load_config,
)
from aox_agent_core.errors import ConfigError

NO_ENVIRONMENT: dict[str, str] = {}


def write_override(tmp_path: Path, content: str) -> Path:
    override = tmp_path / "agent-core.toml"
    override.write_text(content, encoding="utf-8")
    return override


def test_defaults_run_in_replay_mode() -> None:
    assert load_config(environ=NO_ENVIRONMENT).mode is Mode.REPLAY


def test_default_tiers_use_the_decided_models() -> None:
    tiers = load_config(environ=NO_ENVIRONMENT).routing.tiers

    assert {tier: tiers[tier].model for tier in Tier} == {
        Tier.SMALL: "claude-haiku-4-5-20251001",
        Tier.MID: "claude-sonnet-5-5",
        Tier.LARGE: "claude-opus-5-5",
    }
    assert {tiers[tier].provider for tier in Tier} == {Provider.ANTHROPIC}


def test_every_default_tier_has_a_price() -> None:
    config = load_config(environ=NO_ENVIRONMENT)

    for tier_config in config.routing.tiers.values():
        price = config.price_for(tier_config.provider, tier_config.model)
        assert price.output_usd_per_mtok > price.input_usd_per_mtok > 0


def test_default_prices_are_dated_and_sourced_per_provider() -> None:
    pricing = load_config(environ=NO_ENVIRONMENT).pricing

    assert set(pricing) == {Provider.ANTHROPIC, Provider.BEDROCK}
    for provider_pricing in pricing.values():
        assert provider_pricing.as_of <= date.today()
        assert provider_pricing.source.startswith("https://")
        assert provider_pricing.models


def test_mode_variable_overrides_the_mode() -> None:
    config = load_config(environ={"AGENT_CORE_MODE": "live"})

    assert config.mode is Mode.LIVE


def test_invalid_mode_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="mode"):
        load_config(environ={"AGENT_CORE_MODE": "production"})


def test_override_replaces_one_tier_and_keeps_the_others(tmp_path: Path) -> None:
    override = write_override(
        tmp_path,
        '[routing.tiers.large]\nprovider = "bedrock"\nmodel = "anthropic.claude-opus-5-5"\n',
    )

    tiers = load_config(override, environ=NO_ENVIRONMENT).routing.tiers

    assert tiers[Tier.LARGE].provider is Provider.BEDROCK
    assert tiers[Tier.LARGE].model == "anthropic.claude-opus-5-5"
    assert tiers[Tier.SMALL].model == "claude-haiku-4-5-20251001"


def test_override_path_is_read_from_the_environment(tmp_path: Path) -> None:
    override = write_override(tmp_path, 'mode = "record"\n')

    config = load_config(environ={"AGENT_CORE_CONFIG": str(override)})

    assert config.mode is Mode.RECORD


def test_tier_without_a_price_is_rejected(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[routing.tiers.mid]\nmodel = "unpriced-model"\n')

    with pytest.raises(ConfigError, match="no entry under pricing"):
        load_config(override, environ=NO_ENVIRONMENT)


def test_unknown_setting_is_rejected(tmp_path: Path) -> None:
    override = write_override(tmp_path, "[routing]\nbudget_per_call = 1\n")

    with pytest.raises(ConfigError, match="budget_per_call"):
        load_config(override, environ=NO_ENVIRONMENT)


def test_missing_override_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read config file"):
        load_config(tmp_path / "absent.toml", environ=NO_ENVIRONMENT)


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    override = write_override(tmp_path, "mode = \n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(override, environ=NO_ENVIRONMENT)


def test_task_map_routes_a_task_to_its_tier(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[routing.tasks]\nextraction = "small"\n')

    routing = load_config(override, environ=NO_ENVIRONMENT).routing

    assert routing.tier_for_task("extraction") is Tier.SMALL


def test_unknown_task_raises_config_error() -> None:
    routing = load_config(environ=NO_ENVIRONMENT).routing

    with pytest.raises(ConfigError, match="synthesis"):
        routing.tier_for_task("synthesis")


def test_task_mapped_to_an_unknown_tier_is_rejected(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[routing.tasks]\nextraction = "tiny"\n')

    with pytest.raises(ConfigError):
        load_config(override, environ=NO_ENVIRONMENT)


def test_budget_can_drop_one_tier_instead_of_raising(tmp_path: Path) -> None:
    override = write_override(
        tmp_path,
        '[routing]\nbudget_usd_per_call = "0.05"\non_budget_exceeded = "drop_one_tier"\n',
    )

    routing = load_config(override, environ=NO_ENVIRONMENT).routing

    assert routing.on_budget_exceeded is BudgetAction.DROP_ONE_TIER
    assert str(routing.budget_usd_per_call) == "0.05"


def test_budget_defaults_to_raising() -> None:
    routing = load_config(environ=NO_ENVIRONMENT).routing

    assert routing.on_budget_exceeded is BudgetAction.RAISE


@pytest.mark.parametrize(
    ("tier", "lower"), [(Tier.LARGE, Tier.MID), (Tier.MID, Tier.SMALL), (Tier.SMALL, None)]
)
def test_one_lower_steps_down_a_tier(tier: Tier, lower: Tier | None) -> None:
    assert tier.one_lower() is lower


def test_routing_must_define_every_tier() -> None:
    document = load_config(environ=NO_ENVIRONMENT).model_dump()
    del document["routing"]["tiers"][Tier.SMALL]

    with pytest.raises(ValidationError, match="missing: small"):
        AgentCoreConfig.model_validate(document)


def test_invalid_secret_pattern_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid regex"):
        ReplayConfig(extra_secret_patterns={"broken": "("})


def test_audit_database_url_is_hidden_in_repr(tmp_path: Path) -> None:
    override = write_override(
        tmp_path, 'audit_database_url = "postgresql://app:hunter2@localhost:4202/audit"\n'
    )

    config = load_config(override, environ=NO_ENVIRONMENT)

    assert "hunter2" not in repr(config)
    assert config.audit_database_url is not None
