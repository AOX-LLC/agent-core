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


def test_audit_url_with_a_password_is_refused_in_a_file(tmp_path: Path) -> None:
    override = write_override(
        tmp_path, '[audit]\ndatabase_url = "postgresql://app:hunter2@localhost:4202/audit"\n'
    )

    with pytest.raises(ConfigError, match="AGENT_CORE_AUDIT_DATABASE_URL") as caught:
        load_config(override, environ=NO_ENVIRONMENT)

    assert "hunter2" not in str(caught.value)


def test_audit_url_without_a_password_is_allowed_in_a_file(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[audit]\ndatabase_url = "sqlite:///audit.sqlite3"\n')

    config = load_config(override, environ=NO_ENVIRONMENT)

    assert config.audit.database_url is not None
    assert config.audit.database_url.get_secret_value() == "sqlite:///audit.sqlite3"


def test_audit_url_comes_from_the_environment_and_stays_hidden() -> None:
    database_url = "postgresql://app:hunter2@localhost:4202/audit"

    config = load_config(environ={"AGENT_CORE_AUDIT_DATABASE_URL": database_url})

    assert config.audit.database_url is not None
    assert config.audit.database_url.get_secret_value() == database_url
    assert "hunter2" not in repr(config)


def test_bedrock_small_tier_has_no_default_model() -> None:
    bedrock = load_config(environ=NO_ENVIRONMENT).bedrock

    assert Tier.SMALL not in bedrock.tier_models
    with pytest.raises(ConfigError, match="small tier has no default Bedrock model"):
        bedrock.model_for(Tier.SMALL)


def test_bedrock_tier_models_are_priced() -> None:
    config = load_config(environ=NO_ENVIRONMENT)

    for tier in (Tier.MID, Tier.LARGE):
        config.price_for(Provider.BEDROCK, config.bedrock.model_for(tier))


def test_unpriced_bedrock_tier_model_is_rejected(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[bedrock.tier_models]\nsmall = "anthropic.unknown"\n')

    with pytest.raises(ConfigError, match=r"pricing\.bedrock"):
        load_config(override, environ=NO_ENVIRONMENT)


def test_relative_cassette_dir_resolves_against_the_config_file(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[replay]\ncassette_dir = "recordings"\n')

    config = load_config(override, environ=NO_ENVIRONMENT)

    assert config.replay.cassette_dir == tmp_path.resolve() / "recordings"


@pytest.mark.parametrize(
    ("base_url", "allowed"),
    [
        ("https://api.anthropic.com", True),
        ("http://127.0.0.1:8080", True),
        ("http://localhost:4210/v1", True),
        ("http://api.example.com", False),
        ("ftp://api.anthropic.com", False),
    ],
)
def test_anthropic_base_url_must_be_https_or_loopback(
    tmp_path: Path, base_url: str, allowed: bool
) -> None:
    override = write_override(tmp_path, f'[anthropic]\nbase_url = "{base_url}"\n')

    if allowed:
        assert load_config(override, environ=NO_ENVIRONMENT).anthropic.base_url == base_url
    else:
        with pytest.raises(ConfigError, match="base_url"):
            load_config(override, environ=NO_ENVIRONMENT)


def test_tier_moved_to_bedrock_takes_the_bedrock_default_model(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[routing.tiers.mid]\nprovider = "bedrock"\n')

    config = load_config(override, environ=NO_ENVIRONMENT)

    mid = config.routing.tiers[Tier.MID]
    assert (mid.provider, mid.model) == (Provider.BEDROCK, "anthropic.claude-sonnet-5-5")
    assert config.price_for(mid.provider, mid.model).input_usd_per_mtok > 0


def test_small_tier_cannot_move_to_bedrock_without_a_model(tmp_path: Path) -> None:
    override = write_override(tmp_path, '[routing.tiers.small]\nprovider = "bedrock"\n')

    with pytest.raises(ConfigError, match="small tier has no default Bedrock model"):
        load_config(override, environ=NO_ENVIRONMENT)


def test_explicit_bedrock_model_is_kept(tmp_path: Path) -> None:
    override = write_override(
        tmp_path,
        '[routing.tiers.large]\nprovider = "bedrock"\nmodel = "anthropic.claude-sonnet-5-5"\n',
    )

    large = load_config(override, environ=NO_ENVIRONMENT).routing.tiers[Tier.LARGE]

    assert large.model == "anthropic.claude-sonnet-5-5"
