from __future__ import annotations

import pytest

from sensitive_guard.paths import PathError, canonical_path


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("orders[0].customer.name", "orders.0.customer.name", "orders[0].customer.name"),
        ("orders.12.items.3.id", "orders[12].items[3].id", "orders[12].items[3].id"),
        ("matrix[0][2].value", "matrix.0.2.value", "matrix[0][2].value"),
        ("[0].name", "0.name", "[0].name"),
        ("a[0].b[1][2]", "a.0.b.1.2", "a[0].b[1][2]"),
        ("a[01].b[0002]", "a.1.b.2", "a[1].b[2]"),
    ],
)
def test_dot_and_bracket_paths_normalize_identically(
    left: str, right: str, expected: str
) -> None:
    assert canonical_path(left) == expected
    assert canonical_path(right) == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "a..b",
        ".a",
        "a.",
        "a[",
        "a[]",
        "a[-1]",
        "a[x]",
        "a[0]b",
        "a.²",
    ],
)
def test_invalid_paths_are_rejected(path: str) -> None:
    with pytest.raises(PathError):
        canonical_path(path)


def test_non_string_path_is_rejected() -> None:
    with pytest.raises(PathError):
        canonical_path(1)  # type: ignore[arg-type]
