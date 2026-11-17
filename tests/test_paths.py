import pytest

from sensitrace.paths import (
    BoundaryError,
    PathError,
    canonical_key,
    normalize,
    parse_path,
    resolve,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a", ("a",)),
        ("a.b.c", ("a", "b", "c")),
        ("a[2].b", ("a", 2, "b")),
        ("users[0].id_number", ("users", 0, "id_number")),
        ("a[0][1]", ("a", 0, 1)),
    ],
)
def test_parse_valid_paths(text, expected):
    assert parse_path(text) == expected
    assert canonical_key(parse_path(text)) == normalize(text)


@pytest.mark.parametrize(
    "text",
    ["", "a.", "1a.b", "a..b", "a[]", "a[x]", "a.b[", ".a", "a.[0]", "a[-1]"],
)
def test_parse_invalid_paths(text):
    with pytest.raises(PathError):
        parse_path(text)


def test_different_spellings_share_canonical_identity():
    # Array index spelling normalises to one canonical key regardless of input.
    assert canonical_key(("users", 0, "id")) == "users[0].id"
    assert normalize("users[0].id") == canonical_key(("users", 0, "id"))


def test_object_then_array_path_is_unambiguous():
    # The two addressing formats compose: member then index then member.
    tokens = parse_path("profile.addresses[1].city")
    assert tokens == ("profile", "addresses", 1, "city")
    assert canonical_key(tokens) == "profile.addresses[1].city"
    # Re-parsing the canonical form is a fixed point.
    assert parse_path(canonical_key(tokens)) == tokens


def test_resolve_walks_data_and_detects_bounds():
    data = {"users": [{"id": 1}, {"id": 2}]}
    assert resolve(data, parse_path("users[1].id")) == 2
    with pytest.raises(BoundaryError):
        resolve(data, parse_path("users[5].id"))
    with pytest.raises(BoundaryError):
        resolve(data, parse_path("users[0].missing"))
