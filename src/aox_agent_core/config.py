"""Configuration: modes, routing tiers, prices, replay, tracing and audit settings.

Model IDs and prices live only in configuration, never in code. The library ships
its defaults as package data (defaults.toml); a project overrides any part of them
with its own TOML file, named by AGENT_CORE_CONFIG or passed to load_config().
"""

import os
import re
import tomllib
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, StringConstraints, ValidationError, model_validator

from aox_agent_core._model import CassetteName, FrozenModel
from aox_agent_core.errors import ConfigError

CONFIG_PATH_ENV = "AGENT_CORE_CONFIG"
MODE_ENV = "AGENT_CORE_MODE"
AUDIT_DATABASE_URL_ENV = "AGENT_CORE_AUDIT_DATABASE_URL"

TaskName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
RuleName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
UsdPerMillionTokens = Annotated[Decimal, Field(ge=0)]


class Mode(StrEnum):
    """How model calls are served.

    REPLAY is the default so that a fresh clone never spends money: live calls
    must be chosen explicitly.
    """

    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class Tier(StrEnum):
    """Routing tiers, from cheapest to most capable."""

    SMALL = "small"
    MID = "mid"
    LARGE = "large"

    def one_lower(self) -> "Tier | None":
        """Return the next cheaper tier, or None for the cheapest."""
        order = list(Tier)
        position = order.index(self)
        return order[position - 1] if position > 0 else None

    def one_higher(self) -> "Tier | None":
        """Return the next more capable tier, or None for the most capable."""
        order = list(Tier)
        position = order.index(self)
        return order[position + 1] if position + 1 < len(order) else None


class Provider(StrEnum):
    """Where a model is served from."""

    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"


class Effort(StrEnum):
    """Values for the API's output_config.effort setting."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class BudgetAction(StrEnum):
    """What the router does when a call's estimated cost is over budget."""

    RAISE = "raise"
    DROP_ONE_TIER = "drop_one_tier"


class SecretAction(StrEnum):
    """What recording does when it finds a secret in a request or response."""

    REFUSE = "refuse"
    REDACT = "redact"


class TierConfig(FrozenModel):
    """The model behind one tier."""

    provider: Provider = Provider.ANTHROPIC
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    max_tokens: Annotated[int, Field(gt=0)] = 16_000
    effort: Effort | None = None


class RoutingConfig(FrozenModel):
    """Tier definitions plus the optional task map and budget rules.

    tasks maps a task name such as "extraction" to a tier, so callers can pass
    task="extraction" instead of naming a tier.

    budget_usd_per_call caps the worst-case spend of a whole call, retries and
    escalation included. When the first attempt's estimate is over budget,
    on_budget_exceeded decides what happens: RAISE raises BudgetExceededError;
    DROP_ONE_TIER retries the estimate one tier lower and raises only if that is
    still over budget or there is no lower tier. A retry or escalation that could
    take the total over budget raises BudgetExceededError instead of being sent.
    """

    tiers: Mapping[Tier, TierConfig]
    default_tier: Tier = Tier.MID
    tasks: Mapping[TaskName, Tier] = Field(default_factory=dict)
    budget_usd_per_call: Annotated[Decimal, Field(gt=0)] | None = None
    on_budget_exceeded: BudgetAction = BudgetAction.RAISE
    escalate_on_structured_failure: bool = False

    @model_validator(mode="after")
    def _every_tier_is_defined(self) -> Self:
        missing = sorted(set(Tier) - set(self.tiers))
        if missing:
            raise ValueError(f"routing.tiers is missing: {', '.join(missing)}")
        return self

    def tier_for_task(self, task: str) -> Tier:
        """Return the tier mapped to a task name."""
        try:
            return self.tasks[task]
        except KeyError:
            raise ConfigError(f"Unknown task {task!r}; add it to routing.tasks.") from None


class ModelPrice(FrozenModel):
    """Prices for one model, in US dollars per million tokens."""

    input_usd_per_mtok: UsdPerMillionTokens
    output_usd_per_mtok: UsdPerMillionTokens
    cache_write_5m_usd_per_mtok: UsdPerMillionTokens
    cache_write_1h_usd_per_mtok: UsdPerMillionTokens | None = None
    cache_read_usd_per_mtok: UsdPerMillionTokens


class ProviderPricing(FrozenModel):
    """One provider's price list, stamped with where and when it was taken."""

    as_of: date
    source: Annotated[str, StringConstraints(pattern=r"^https://\S+$")]
    region: str | None = None
    models: Mapping[str, ModelPrice] = Field(default_factory=dict)


class ReplayConfig(FrozenModel):
    """Where recordings live and how secrets found while recording are handled.

    extra_secret_patterns adds project-specific rules, as rule name to regex, on
    top of the built-in ones.
    """

    cassette_dir: Path = Path("replays")
    cassette: CassetteName = "default"
    on_secret: SecretAction = SecretAction.REFUSE
    extra_secret_patterns: Mapping[RuleName, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _patterns_compile(self) -> Self:
        for rule, pattern in self.extra_secret_patterns.items():
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(
                    f"secret pattern {rule!r} is not a valid regex: {error}"
                ) from error
        return self


class TracingConfig(FrozenModel):
    """Tracing options. Prompt and completion text stay out of spans unless enabled."""

    capture_content: bool = False


class AnthropicConfig(FrozenModel):
    """How to reach the Anthropic API.

    base_url is always passed to the SDK explicitly, so ANTHROPIC_BASE_URL in the
    environment can never redirect the library's traffic. Plain http is accepted
    only for a loopback address, for local test servers.
    """

    base_url: Annotated[
        str,
        StringConstraints(pattern=r"^(https://\S+|http://(127\.0\.0\.1|localhost)(:\d+)?(/\S*)?)$"),
    ] = "https://api.anthropic.com"
    timeout_seconds: Annotated[float, Field(gt=0)] = 600.0
    max_retries: Annotated[int, Field(ge=0, le=10)] = 2


class BedrockConfig(FrozenModel):
    """Amazon Bedrock region and the Bedrock model ID for each tier.

    When a project's config sets a tier's provider to "bedrock" without naming a
    model, load_config fills in that tier's model from tier_models. A tier
    missing from tier_models has no Bedrock default, and moving it to Bedrock
    without a model is a ConfigError.
    """

    region: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")] = "us-east-1"
    tier_models: Mapping[Tier, Annotated[str, StringConstraints(min_length=1)]] = Field(
        default_factory=dict
    )

    def model_for(self, tier: Tier) -> str:
        """Return the Bedrock model ID for a tier, or raise ConfigError if it has none."""
        try:
            return self.tier_models[tier]
        except KeyError:
            raise ConfigError(
                f"The {tier.value} tier has no default Bedrock model; set "
                f"bedrock.tier_models.{tier.value} and price it under pricing.bedrock."
            ) from None


class AuditConfig(FrozenModel):
    """Where the audit log is stored: a sqlite:/// or postgresql:// URL.

    A URL carrying a password belongs in AGENT_CORE_AUDIT_DATABASE_URL, never in
    a config file a project commits; load_config refuses one found in a file.
    """

    database_url: SecretStr | None = None


class AgentCoreConfig(FrozenModel):
    """The library's whole configuration."""

    mode: Mode = Mode.REPLAY
    routing: RoutingConfig
    pricing: Mapping[Provider, ProviderPricing]
    replay: ReplayConfig = ReplayConfig()
    tracing: TracingConfig = TracingConfig()
    audit: AuditConfig = AuditConfig()
    anthropic: AnthropicConfig = AnthropicConfig()
    bedrock: BedrockConfig = BedrockConfig()

    @model_validator(mode="after")
    def _every_tier_is_priced(self) -> Self:
        for tier, tier_config in self.routing.tiers.items():
            if not self.has_price(tier_config.provider, tier_config.model):
                raise ValueError(
                    f"tier {tier.value!r} uses {tier_config.provider.value} model "
                    f"{tier_config.model!r}, which has no entry under pricing"
                )
        for tier, model in self.bedrock.tier_models.items():
            if not self.has_price(Provider.BEDROCK, model):
                raise ValueError(
                    f"bedrock.tier_models.{tier.value} is {model!r}, which has no entry "
                    "under pricing.bedrock"
                )
        return self

    def price_for(self, provider: Provider, model: str) -> ModelPrice:
        """Return the configured price of a model."""
        if not self.has_price(provider, model):
            raise ConfigError(f"No price configured for {provider.value} model {model!r}.")
        return self.pricing[provider].models[model]

    def has_price(self, provider: Provider, model: str) -> bool:
        provider_pricing = self.pricing.get(provider)
        return provider_pricing is not None and model in provider_pricing.models


def load_config(
    path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> AgentCoreConfig:
    """Load the packaged defaults, apply a project's overrides, and validate.

    The override file is `path` if given, else the file named by AGENT_CORE_CONFIG.
    It is merged over the defaults table by table, so a project can replace a
    single tier without restating the rest. AGENT_CORE_MODE and
    AGENT_CORE_AUDIT_DATABASE_URL, when set, override the file last.
    """
    env = os.environ if environ is None else environ
    document = _read_default_document()

    override_path = path if path is not None else _override_path_from(env)
    if override_path is not None:
        override = _read_toml(override_path)
        _reject_password_in_audit_url(override, override_path)
        _anchor_cassette_dir(override, override_path)
        bedrock_tiers_without_model = _bedrock_tiers_without_model(override)
        document = _merge_tables(document, override)
        _fill_bedrock_models(document, bedrock_tiers_without_model)

    mode_override = env.get(MODE_ENV, "").strip()
    if mode_override:
        document["mode"] = mode_override

    audit_url_override = env.get(AUDIT_DATABASE_URL_ENV, "").strip()
    if audit_url_override:
        document["audit"] = {**document.get("audit", {}), "database_url": audit_url_override}

    try:
        return AgentCoreConfig.model_validate(document)
    except ValidationError as error:
        raise ConfigError(f"Invalid agent-core configuration:\n{error}") from error


def _bedrock_tiers_without_model(override: dict[str, Any]) -> list[str]:
    """Tiers the override moves to Bedrock without naming a model."""
    routing = override.get("routing")
    tiers = routing.get("tiers") if isinstance(routing, dict) else None
    if not isinstance(tiers, dict):
        return []
    return [
        tier
        for tier, table in tiers.items()
        if isinstance(table, dict)
        and table.get("provider") == Provider.BEDROCK.value
        and "model" not in table
    ]


def _fill_bedrock_models(document: dict[str, Any], tiers: list[str]) -> None:
    """Give each tier moved to Bedrock without a model its bedrock.tier_models default."""
    defaults = document.get("bedrock", {}).get("tier_models", {})
    for tier in tiers:
        if tier not in defaults:
            raise ConfigError(
                f"routing.tiers.{tier} moves to Bedrock without a model, and the {tier} tier "
                f"has no default Bedrock model; set routing.tiers.{tier}.model and price it "
                "under pricing.bedrock."
            )
        document["routing"]["tiers"][tier]["model"] = defaults[tier]


def _anchor_cassette_dir(override: dict[str, Any], path: Path) -> None:
    """Resolve a relative replay.cassette_dir against the config file's folder.

    Without this the directory would depend on where the process was started.
    """
    replay_table = override.get("replay")
    if not isinstance(replay_table, dict):
        return
    cassette_dir = replay_table.get("cassette_dir")
    if isinstance(cassette_dir, str) and not Path(cassette_dir).is_absolute():
        replay_table["cassette_dir"] = str(path.resolve().parent / cassette_dir)


def _reject_password_in_audit_url(override: dict[str, Any], path: Path) -> None:
    audit_table = override.get("audit")
    database_url = audit_table.get("database_url") if isinstance(audit_table, dict) else None
    if isinstance(database_url, str) and urlsplit(database_url).password is not None:
        raise ConfigError(
            f"{path} puts a password in audit.database_url; set {AUDIT_DATABASE_URL_ENV} instead."
        )


def _override_path_from(env: Mapping[str, str]) -> Path | None:
    configured = env.get(CONFIG_PATH_ENV, "").strip()
    return Path(configured) if configured else None


def _read_default_document() -> dict[str, Any]:
    defaults = resources.files("aox_agent_core").joinpath("defaults.toml")
    return tomllib.loads(defaults.read_text(encoding="utf-8"))


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    except OSError as error:
        raise ConfigError(f"Cannot read config file {path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Config file {path} is not valid TOML: {error}") from error


def _merge_tables(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override into base recursively; anything but a table is replaced whole."""
    merged = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = _merge_tables(base_value, override_value)
        else:
            merged[key] = override_value
    return merged
