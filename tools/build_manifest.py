"""Rebuild samples/<name>/manifest.json using the engine digest rules."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SAMPLE_FILES = ("services.json", "pool.json", "requests.json", "policy.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample_dir", type=Path)
    args = parser.parse_args()

    file_hashes: dict[str, str] = {}
    components: list[bytes] = []
    for name in SAMPLE_FILES:
        raw = (args.sample_dir / name).read_bytes()
        file_hashes[name] = "sha256:" + hashlib.sha256(raw).hexdigest()
        components.extend((name.encode("utf-8"), raw))
    scenario_digest = (
        "sha256:" + hashlib.sha256(b"\0".join(components)).hexdigest()
    )
    manifest = {
        "format": "vmpool-sample/1",
        "scenario_digest": scenario_digest,
        "files": file_hashes,
    }
    (args.sample_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(scenario_digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
