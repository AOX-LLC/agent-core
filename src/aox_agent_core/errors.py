"""Every error the library raises, rooted at AgentCoreError.

The hierarchy is public API: consumers catch these classes, so a class is never
renamed or moved to a different parent without a major version.
"""


class AgentCoreError(Exception):
    """Base class for every error raised by aox_agent_core."""


class ConfigError(AgentCoreError):
    """The configuration is missing, unreadable or invalid."""


class MissingCredentialsError(AgentCoreError):
    """Live or record mode needs an API key and none was given."""


class EventLoopRunningError(AgentCoreError, RuntimeError):
    """A synchronous helper was called from inside a running event loop."""


class RoutingError(AgentCoreError):
    """A call could not be routed to a tier."""


class BudgetExceededError(RoutingError):
    """The estimated cost of a call is over the configured per-call budget."""


class ProviderError(AgentCoreError):
    """The model provider failed. The provider SDK's error is chained as __cause__."""


class RateLimitedError(ProviderError):
    """The provider rejected the call because of a rate limit."""


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached or returned a server error."""


class ProviderRequestError(ProviderError):
    """The provider rejected the request as invalid."""


class ModelRefusalError(AgentCoreError):
    """The model declined the request (stop reason 'refusal')."""


class StructuredOutputError(AgentCoreError):
    """The model's output did not validate against the requested schema after every attempt."""


class ReplayError(AgentCoreError):
    """Base class for record/replay failures."""


class ReplayMissError(ReplayError):
    """Replay mode found no recorded response for a request."""


class CassetteFormatError(ReplayError):
    """A cassette file is unreadable, malformed or of an unsupported format version."""


class SecretInRecordingError(ReplayError):
    """A secret was found in a recording, so the recording was not written."""


class ApprovalError(AgentCoreError):
    """Base class for approval-queue failures."""


class NotAuthorizedToResolveError(ApprovalError):
    """The principal is not allowed to resolve this approval request."""


class ApprovalAlreadyResolvedError(ApprovalError):
    """The approval request was already approved, rejected, expired or cancelled."""


class ApprovalExpiredError(ApprovalError):
    """The approval request expired before it was resolved or used."""


class ApprovalPayloadMismatchError(ApprovalError):
    """The action payload differs from the payload that was approved."""


class AuditError(AgentCoreError):
    """Base class for audit-log failures."""


class AuditIntegrityError(AuditError):
    """The audit chain failed verification or does not match the expected head."""


class AuditWriteError(AuditError):
    """An audit record could not be written."""


class AuditPayloadRejectedError(AuditError):
    """An audit payload contained a forbidden key, a secret, or was too large."""


class EvalError(AgentCoreError):
    """An eval suite could not be loaded or run."""
