from decimal import Decimal

from aox_agent_core.config import ModelPrice
from aox_agent_core.models import Usage
from aox_agent_core.models.pricing import cost_of, estimate_input_tokens, worst_case_cost

PRICE = ModelPrice(
    input_usd_per_mtok=Decimal("2.00"),
    output_usd_per_mtok=Decimal("10.00"),
    cache_write_5m_usd_per_mtok=Decimal("2.50"),
    cache_write_1h_usd_per_mtok=Decimal("4.00"),
    cache_read_usd_per_mtok=Decimal("0.20"),
)


def test_cost_covers_every_token_kind_exactly() -> None:
    usage = Usage(
        input_tokens=1_000,
        output_tokens=500,
        cache_creation_input_tokens=2_000,
        cache_read_input_tokens=10_000,
    )

    # 1000*2 + 500*10 + 2000*2.5 + 10000*0.2 = 14000 per million
    assert cost_of(usage, PRICE) == Decimal("0.014")


def test_worst_case_ignores_caching() -> None:
    assert worst_case_cost(1_000, 1_000, PRICE) == Decimal("0.012")


def test_input_estimate_rounds_up() -> None:
    assert estimate_input_tokens("abcd", None, "ef") == 2
    assert estimate_input_tokens() == 0
