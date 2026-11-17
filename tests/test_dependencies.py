def test_numpy_and_pytest_are_available():
    import numpy
    import pytest

    assert numpy.__version__
    assert pytest.__version__

