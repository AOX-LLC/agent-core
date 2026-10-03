import inspect

import pytest

from aox_agent_core import errors
from aox_agent_core.errors import (
    AgentCoreError,
    ApprovalError,
    AuditError,
    ProviderError,
    ReplayError,
    RoutingError,
)

EXPECTED_PARENTS: dict[type[Exception], type[Exception]] = {
    errors.ConfigError: AgentCoreError,
    errors.MissingCredentialsError: AgentCoreError,
    errors.EventLoopRunningError: AgentCoreError,
    errors.RoutingError: AgentCoreError,
    errors.BudgetExceededError: RoutingError,
    errors.ProviderError: AgentCoreError,
    errors.RateLimitedError: ProviderError,
    errors.ProviderUnavailableError: ProviderError,
    errors.ProviderRequestError: ProviderError,
    errors.AttachmentError: AgentCoreError,
    errors.PromptError: AgentCoreError,
    errors.ModelRefusalError: AgentCoreError,
    errors.StructuredOutputError: AgentCoreError,
    errors.ReplayError: AgentCoreError,
    errors.ReplayMissError: ReplayError,
    errors.StaleRecordingError: ReplayError,
    errors.CassetteFormatError: ReplayError,
    errors.SecretInRecordingError: ReplayError,
    errors.ApprovalError: AgentCoreError,
    errors.ApprovalNotFoundError: ApprovalError,
    errors.NotAuthorizedToResolveError: ApprovalError,
    errors.NotTheRequesterError: ApprovalError,
    errors.ApprovalAlreadyResolvedError: ApprovalError,
    errors.ApprovalNotGrantedError: ApprovalError,
    errors.ApprovalExpiredError: ApprovalError,
    errors.ApprovalPayloadMismatchError: ApprovalError,
    errors.AuditError: AgentCoreError,
    errors.AuditIntegrityError: AuditError,
    errors.AuditWriteError: AuditError,
    errors.AuditPayloadRejectedError: AuditError,
    errors.EvalError: AgentCoreError,
}


@pytest.mark.parametrize(
    ("error", "parent"), EXPECTED_PARENTS.items(), ids=lambda value: value.__name__
)
def test_error_has_its_documented_parent(error: type[Exception], parent: type[Exception]) -> None:
    assert error.__bases__[0] is parent


def test_every_error_class_is_covered() -> None:
    defined = {
        cls
        for _, cls in inspect.getmembers(errors, inspect.isclass)
        if issubclass(cls, AgentCoreError) and cls is not AgentCoreError
    }

    assert defined == set(EXPECTED_PARENTS)


def test_event_loop_error_is_also_a_runtime_error() -> None:
    assert issubclass(errors.EventLoopRunningError, RuntimeError)
