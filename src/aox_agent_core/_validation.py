"""Validation-context markers the library passes to its own models.

They are objects, not strings, so data cannot carry one and a caller cannot
set one by accident; only code that imports this private module can.
"""

from typing import Final

# from_bytes() has just hashed these bytes itself, so the validator need not.
HASHED_ATTACHMENT: Final = object()
# The record was read back from storage; it was checked against the policy
# (sizes, secret-shaped names and values) when written, and is not again, so a
# rule added later cannot make an existing audit chain unreadable.
STORED_RECORD: Final = object()
