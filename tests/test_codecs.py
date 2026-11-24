from perm_audit.codecs import (
    CANONICAL_MASK,
    parse_integer_mask,
    parse_named_mask,
    parse_octal_mask,
)
from perm_audit.codes import E_MASK_SYNTAX, E_PERMISSION_NAME
from perm_audit.codecs import MaskParseError


def test_integer_exact_and_high_bit_bound():
    exact = parse_integer_mask(0xABC)
    assert exact.observed_mask == 0xABC
    assert exact.error_bound == 0

    high = parse_integer_mask(0x1000 | 0x1)
    assert high.observed_mask == 1
    assert high.retained_high_mask == 0x1000
    assert high.uncertain_mask == CANONICAL_MASK
    assert "high_bits_retained" in high.formula


def test_octal_digit_count_truncation_bound():
    full = parse_octal_mask("0o0001")
    assert full.observed_mask == 1
    assert full.error_bound == 0

    truncated = parse_octal_mask("4")
    assert truncated.observed_mask == 4
    assert truncated.uncertain_mask == 0xFF8
    assert truncated.error_bound == 0xFF8
    assert "digit_count=1" in truncated.formula


def test_named_missing_bits_are_zero_filled_and_bounded():
    value = parse_named_mask(["read"])
    assert value.observed_mask == 1
    assert value.uncertain_mask == (CANONICAL_MASK ^ 1)
    assert value.error_bound == CANONICAL_MASK ^ 1

    full = parse_named_mask(["full"])
    assert full.observed_mask == CANONICAL_MASK
    assert full.error_bound == 0


def test_invalid_permission_writings_have_codes():
    try:
        parse_octal_mask("89")
    except MaskParseError as exc:
        assert exc.code == E_MASK_SYNTAX
    else:
        raise AssertionError("invalid octal accepted")

    try:
        parse_named_mask(["not-a-real-right"])
    except MaskParseError as exc:
        assert exc.code == E_PERMISSION_NAME
    else:
        raise AssertionError("invalid named right accepted")
