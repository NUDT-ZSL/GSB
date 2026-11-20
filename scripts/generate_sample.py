#!/usr/bin/env python3
from pathlib import Path

from tls_trust_pipeline.samplegen import generate_default_sample

if __name__ == "__main__":
    summary = generate_default_sample(Path(__file__).resolve().parents[1])
    print(f"generated {summary['cert_count']} certificates and deterministic snapshots")

