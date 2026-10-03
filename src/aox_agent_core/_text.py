"""Free text that a person or a terminal will read, and the characters it may not carry.

Control characters (Cc), format characters such as the bidirectional overrides and
zero-width marks (Cf), and the line and paragraph separators (Zl, Zp) can reorder
or hide what a UI shows an approver, or drive a terminal. Text written by a
requester or an approver is refused if it holds any of them; text already stored
with them is shown with each replaced by U+FFFD.
"""

import unicodedata
from typing import Final

REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
REPLACEMENT: Final = "�"


def has_unsafe_characters(text: str) -> bool:
    """True when `text` holds a control, format, line-separator or paragraph-separator character."""
    return any(unicodedata.category(character) in REFUSED_CATEGORIES for character in text)


def neutralized(text: str) -> str:
    """`text` with each such character replaced by U+FFFD, for display."""
    return "".join(
        REPLACEMENT if unicodedata.category(character) in REFUSED_CATEGORIES else character
        for character in text
    )


def require_safe_text(text: str) -> str:
    """Return `text`, or raise ValueError if it holds a refused character."""
    if has_unsafe_characters(text):
        raise ValueError("text must not contain control or bidirectional formatting characters")
    return text


def check_short_text(text: str, *, what: str) -> str:
    """`text` if it is 1 to 500 characters with no refused character, else ValueError."""
    if not 0 < len(text) <= 500:
        raise ValueError(f"{what} must be 1 to 500 characters")
    if has_unsafe_characters(text):
        raise ValueError(f"{what} must not contain control or bidirectional formatting characters")
    return text
