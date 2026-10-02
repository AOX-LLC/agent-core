"""Turning token counts into dollars with the configured, dated price table."""

import math
import re
from collections.abc import Iterable
from decimal import Decimal

from aox_agent_core.config import ModelPrice
from aox_agent_core.models.attachments import Attachment
from aox_agent_core.models.types import Usage

TOKENS_PER_MILLION = Decimal(1_000_000)

# A deliberately pessimistic characters-per-token ratio for budget checks, which
# run before any call and cannot ask the API to count tokens.
CHARACTERS_PER_TOKEN_ESTIMATE = 3

# Pessimistic per-attachment token counts. The API scales an image down to its
# resolution limit, which bounds its tokens; a PDF page costs its text plus an
# image of the page.
IMAGE_TOKENS_ESTIMATE = 5_000
PDF_PAGE_TOKENS_ESTIMATE = 8_000

_PDF_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def cost_of(usage: Usage, price: ModelPrice) -> Decimal:
    """Return the exact cost in US dollars of one call's usage.

    Cache writes are priced at the 5-minute rate, the API's default cache
    lifetime. The 1-hour rate is never applied.
    """
    weighted_tokens = (
        usage.input_tokens * price.input_usd_per_mtok
        + usage.output_tokens * price.output_usd_per_mtok
        + usage.cache_creation_input_tokens * price.cache_write_5m_usd_per_mtok
        + usage.cache_read_input_tokens * price.cache_read_usd_per_mtok
    )
    return weighted_tokens / TOKENS_PER_MILLION


def worst_case_cost(input_tokens: int, max_output_tokens: int, price: ModelPrice) -> Decimal:
    """Return the most a call can cost: every input token uncached, every output token used."""
    weighted_tokens = (
        input_tokens * price.input_usd_per_mtok + max_output_tokens * price.output_usd_per_mtok
    )
    return weighted_tokens / TOKENS_PER_MILLION


def estimate_input_tokens(*texts: str | None) -> int:
    """Over-estimate the tokens in some text, for budget checks before a call."""
    characters = sum(len(text) for text in texts if text)
    return math.ceil(characters / CHARACTERS_PER_TOKEN_ESTIMATE)


def estimate_attachment_tokens(attachments: Iterable[Attachment]) -> int:
    """Over-estimate the tokens attachments add, for budget checks before a call.

    A PDF's pages are counted from its page objects. An attachment without its
    bytes counts as one image or one page.
    """
    tokens = 0
    for attachment in attachments:
        if attachment.media_type != "application/pdf":
            tokens += IMAGE_TOKENS_ESTIMATE
            continue
        pages = len(_PDF_PAGE_OBJECT.findall(attachment.data)) if attachment.data else 0
        tokens += max(pages, 1) * PDF_PAGE_TOKENS_ESTIMATE
    return tokens
