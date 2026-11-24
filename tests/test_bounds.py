from memguard import codes
from memguard.judge import judge_access
from memguard.model import RegionDecl, TraceRecord


def _regions():
    decls = [
        RegionDecl("ROOT", 1, False, 4096, 0, 4096, 0, None, None),
        RegionDecl("MID", 1, False, 4096, 256, 2048, 0, None, "ROOT"),
        RegionDecl("LEAF", 1, False, 4096, 512, 768, 0, 1000, "MID"),
    ]
    return {d.region_id: d for d in decls}


def _rec(base, size, ts=0, region="LEAF"):
    return TraceRecord("t", 0, region, base, size, "read", 4096, ts)


def test_clean_access():
    assert judge_access(_rec(512, 16), _regions()) is None
    assert judge_access(_rec(768 - 16, 16), _regions()) is None


def test_bound_over_inside_ancestors():
    # 760-792: beyond LEAF end (768) but fully inside MID.
    assert judge_access(_rec(760, 32), _regions()) == codes.E_BOUND_OVER
    # below own start, inside ancestors
    assert judge_access(_rec(500, 8), _regions()) == codes.E_BOUND_OVER


def test_ancestor_over():
    # 2000-2064 crosses MID upper bound (2048).
    assert judge_access(_rec(2000, 64), _regions()) == codes.E_ANCESTOR_OVER
    # beyond the arena itself (no-parent root bound)
    assert judge_access(_rec(4090, 16, region="ROOT"), _regions()) == codes.E_ANCESTOR_OVER


def test_after_life_precedence():
    # in-bounds but after lifecycle end
    assert judge_access(_rec(600, 16, ts=1001), _regions()) == codes.E_AFTER_LIFE
    # out-of-bounds AND after life: lifecycle wins by documented precedence
    assert judge_access(_rec(2000, 64, ts=2000), _regions()) == codes.E_AFTER_LIFE
    # exactly at life end is still alive
    assert judge_access(_rec(600, 16, ts=1000), _regions()) is None


def test_zero_size_access_is_clean():
    assert judge_access(_rec(768, 0), _regions()) is None
