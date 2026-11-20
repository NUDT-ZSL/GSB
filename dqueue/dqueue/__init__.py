"""Local deterministic delayed-queue middleware.

All time judgements depend solely on an injectable logical clock in
milliseconds; the package never reads the system wall clock for scheduling.
"""

__version__ = "1.0.0"
