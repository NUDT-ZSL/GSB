"""gbtdiag - deterministic GBT regression with diagnostics and injection.

The package exposes four domain entry points:

* :func:`train`          - deterministic round-by-round training;
* :func:`predict`        - inference from stored tree contributions;
* :func:`inject_diagnose` - four-stage anomaly injection and attribution;
* :func:`validate`       - reproducibility, recomputation and tolerance.
"""

from .api import inject_diagnose, predict, train, validate
from .config import TrainConfig
from .data import Dataset, make_regression_dataset
from .errors import DomainError, ErrorCode
from .injection import InjectionSpec, make_injection

__version__ = "1.0.0"

__all__ = [
    "train",
    "predict",
    "inject_diagnose",
    "validate",
    "make_regression_dataset",
    "TrainConfig",
    "Dataset",
    "InjectionSpec",
    "make_injection",
    "DomainError",
    "ErrorCode",
    "__version__",
]
