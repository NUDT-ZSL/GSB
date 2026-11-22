from __future__ import annotations

from fractions import Fraction

from ic_ledger.fixedpoint import convert_fraction, frames_to_ticks


def test_conversion_is_deterministic_and_carry_participates() -> None:
    first = convert_fraction(Fraction(10, 3), Fraction(0))
    assert first.rounded_units == 3
    assert first.residue_after == Fraction(1, 3)
    assert first.discarded == Fraction(1, 3)

    second = convert_fraction(Fraction(10, 3), first.residue_after)
    assert second.rounded_units == 3
    assert second.residue_before == Fraction(1, 3)
    assert second.residue_after == Fraction(2, 3)

    third = convert_fraction(Fraction(10, 3), second.residue_after)
    assert third.rounded_units == 4
    assert third.residue_after == Fraction(0)


def test_frames_to_ticks_repeats_bit_for_bit() -> None:
    interval = Fraction("1.3333")
    first = frames_to_ticks(7, interval, 1000, Fraction(0))
    second = frames_to_ticks(7, interval, 1000, Fraction(0))
    assert first == second
    assert first.rounded_units == 9333
    assert first.residue_after == Fraction(1, 10)
    assert first.exact_num == 93331
    assert first.exact_den == 10
    assert first.rounding == "floor"


def test_scale_one_exposes_material_residue() -> None:
    conversion = frames_to_ticks(3, Fraction("1.5"), 1, Fraction(0))
    assert conversion.rounded_units == 4
    assert conversion.discarded == Fraction(1, 2)
    next_conversion = frames_to_ticks(3, Fraction("1.5"), 1, conversion.residue_after)
    assert next_conversion.rounded_units == 5
    assert next_conversion.residue_after == Fraction(0)

