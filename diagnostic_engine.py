#!/usr/bin/env python3
"""Convenience import facade for the diagnostic engine."""

from emotion_diag.anomalies import detect_turn_anomalies
from emotion_diag.cache import ContentCache
from emotion_diag.config import EngineConfig, load_config
from emotion_diag.errors import DiagnosticError, ErrorCode
from emotion_diag.features import extract_features
from emotion_diag.incremental import run_generation_update, run_truth_correction
from emotion_diag.ledger import read_ledger
from emotion_diag.migrate import LedgerMigrator
from emotion_diag.pipeline import run_pipeline
from emotion_diag.samplegen import generate_sample_bundle
from emotion_diag.scheduler import run_state_machine

__all__ = [
    "detect_turn_anomalies",
    "ContentCache",
    "EngineConfig",
    "load_config",
    "DiagnosticError",
    "ErrorCode",
    "extract_features",
    "run_generation_update",
    "run_truth_correction",
    "read_ledger",
    "LedgerMigrator",
    "run_pipeline",
    "generate_sample_bundle",
    "run_state_machine",
]


if __name__ == "__main__":
    from run import main

    raise SystemExit(main())
