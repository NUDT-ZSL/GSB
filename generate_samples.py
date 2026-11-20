"""Regenerate the deterministic local JSONL manifest."""

from pathlib import Path

from embedding_ops.cli import DEFAULT_JOBS, ensure_sample_manifest


if __name__ == "__main__":
    path = Path(DEFAULT_JOBS)
    specs = ensure_sample_manifest(path)
    print(f"wrote {len(specs)} jobs to {path}")
