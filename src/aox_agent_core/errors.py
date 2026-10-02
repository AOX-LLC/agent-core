"""The library's errors, rooted at AgentCoreError.

Operational failures (configuration, credentials, providers, replay, approvals,
audit, evals) raise these classes. Two other exceptions are part of the
contract: building a library model from invalid input raises
pydantic.ValidationError, and a bad argument to a constructor raises ValueError.

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
    """The model's output did not validate against the requested schema after every attempt.

    attempts holds one short validation summary per failed attempt, oldest first.
    The model's raw output is not included.
    """

    def __init__(self, message: str, *, attempts: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.attempts = attempts


class ReplayError(AgentCoreError):
    """Base class for record/replay failures."""


class ReplayMissError(ReplayError):
    """Replay mode found no recorded response for a request."""


class CassetteFormatError(ReplayError):
    """A cassette file is unreadable, malformed or of an unsupported format version."""


class CassetteConflictError(ReplayError):
    """Another recorder wrote the same cassette during this run, so it was not overwritten."""


class SecretInRecordingError(ReplayError):
    """A secret was found in a recording, so the recording was not written."""


class ApprovalError(AgentCoreError):
    """Base class for approval-queue failures."""


class NotAuthorizedToResolveError(ApprovalError):
    """The principal is not allowed to resolve this approval request."""


class ApprovalAlreadyResolvedError(ApprovalError):
    """The request is no longer open: already resolved, consumed or cancelled."""


class ApprovalNotGrantedError(ApprovalError):
    """The action was attempted while its request was pending or after it was rejected."""


class ApprovalExpiredError(ApprovalError):
    """The approval request expired before it was resolved or used."""


class ApprovalPayloadMismatchError(ApprovalError):
    """The action or its payload differs from what was approved."""


class AuditError(AgentCoreError):
    """Base class for audit-log failures."""


class AuditIntegrityError(AuditError):
    """The audit chain failed verification or does not match the expected head."""


class AuditWriteError(AuditError):
    """An audit record could not be written."""


class AuditPayloadRejectedError(AuditError):
    """A payload string looked like a secret when the event was appended.

    Forbidden keys, floats and oversized payloads are caught earlier, as a
    ValidationError when the AuditEvent is built.
    """


class EvalError(AgentCoreError):
    """An eval suite could not be loaded or run."""
