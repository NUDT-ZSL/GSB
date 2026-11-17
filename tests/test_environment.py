from __future__ import annotations


def test_dev_dependencies_are_available():
    import numpy
    import pytest

    assert pytest.__version__
    assert numpy.__version__
