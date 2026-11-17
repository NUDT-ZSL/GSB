from __future__ import annotations

import pytest

from sensitive_flow.errors import E_PATH, E_PATH_INDEX, TraceError
from sensitive_flow.paths import canonical_and_validate, canonicalize_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("customer.id", "customer.id"),
        ("orders[0].contact", "orders[0].contact"),
        ("orders.0.contact", "orders[0].contact"),
        ("$.orders[1].items[0].id", "orders[1].items[0].id"),
        ("/orders/1/contact", "orders[1].contact"),
    ],
)
def test_path_normalization(raw: str, expected: str) -> None:
    assert canonicalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "a..b",
        "a.",
        "a[-1]",
        "a[one]",
        "a[0",
        "a[0]b",
        "a b",
    ],
)
def test_invalid_paths_are_rejected(raw: str) -> None:
    with pytest.raises(TraceError) as caught:
        canonicalize_path(raw)
    assert caught.value.code == E_PATH


def test_array_bounds_use_canonical_keys() -> None:
    assert (
        canonical_and_validate("orders.1.items[0].id", {"orders": 2, "orders[1].items": 1})
        == "orders[1].items[0].id"
    )
    with pytest.raises(TraceError) as caught:
        canonical_and_validate("orders.2.contact", {"orders": 2})
    assert caught.value.code == E_PATH_INDEX
