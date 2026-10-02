"""Span and attribute names. Dashboards depend on them, so they are public API.

The gen_ai.* names follow the OpenTelemetry GenAI semantic conventions; the
agent_core.* names cover what those conventions do not.

No attribute ever carries a credential, a header, or prompt or completion text.
Content capture is opt-in (TracingConfig.capture_content) and goes through the
secret scrubber first.
"""

SPAN_MODEL_CALL = "agent_core.model_call"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

AGENT_CORE_TIER = "agent_core.tier"
AGENT_CORE_REQUESTED_TIER = "agent_core.requested_tier"
AGENT_CORE_TASK = "agent_core.task"
AGENT_CORE_MODE = "agent_core.mode"
AGENT_CORE_ROUTE_REASON = "agent_core.route_reason"
AGENT_CORE_COST_USD = "agent_core.cost_usd"
AGENT_CORE_CACHE_CREATION_INPUT_TOKENS = "agent_core.usage.cache_creation_input_tokens"
AGENT_CORE_CACHE_READ_INPUT_TOKENS = "agent_core.usage.cache_read_input_tokens"
AGENT_CORE_STRUCTURED_ATTEMPTS = "agent_core.structured.attempts"
