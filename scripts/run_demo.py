"""End to end deterministic demonstration of every acceptance scenario.

Runs entirely offline against freshly generated samples and a throwaway
SQLite database under the system temp directory.
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scaler.cli import build_context
from scaler.orchestrator import Orchestrator
from scripts.generate_samples import generate


def heading(text):
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def main():
    workspace = tempfile.mkdtemp(prefix="scaler_demo_")
    sample_dir = os.path.join(workspace, "samples")
    db_path = os.path.join(workspace, "state.db")
    generate(sample_dir)
    print("samples generated at", sample_dir)

    orch = Orchestrator(db_path)
    context, manifest = build_context(sample_dir)
    hashes = [shard["shard_hash"] for shard in manifest["shards"]]

    heading("1. package load: full replace / append / no-op classification")
    print(json.dumps(orch.load_package(
        context, manifest["version"], manifest["generation_floor"], hashes
    ), indent=2))
    print("repeat load ->", orch.load_package(
        context, manifest["version"], manifest["generation_floor"], hashes
    )["mode"])

    heading("2. bounded parallel reconcile over multiple logical rounds")
    for _ in range(6):
        result = orch.reconcile(build_context(sample_dir)[0])
    for row in result["results"]:
        print(row["service"], row["status"], row.get("outcome"),
              "target=", row.get("target"), "sensitive=", row.get("sensitive"))

    heading("3. status with stable ordering and precision markers")
    status = orch.status(build_context(sample_dir)[0])
    print(json.dumps(status, indent=2, sort_keys=True))

    heading("4. incremental patch invalidation closure")
    patch = {
        "gear_changes": [
            {"service": "gateway", "old_ratio": "0.5", "new_ratio": "0.65"}
        ],
        "dependency_changes": [],
        "tombstones": [],
    }
    closure = orch.apply_patch(build_context(sample_dir)[0], patch)
    print("recompute:", closure["affected_closure"])
    print("reuse:", [row["service"] for row in closure["reuse"]])

    heading("5. forced termination and idempotent resume")
    crashed_round = result["round_no"] + 1
    clock = result["logical_clock_ms"] + 1000
    orch.storage.begin_round(crashed_round, clock, 750)
    orch.storage.connection.execute(
        "INSERT INTO actions VALUES(?, 'auth', ?, 'fd-a', 'pending', "
        "'running', '{}')",
        (crashed_round, manifest["version"]),
    )
    first = orch.resume(build_context(sample_dir)[0])
    second = orch.resume(build_context(sample_dir)[0])
    print("first resume:", first)
    print("second resume (must be empty):", second)

    heading("6. verify durable state against recomputation")
    verification = orch.verify(build_context(sample_dir)[0])
    print(json.dumps(verification, indent=2))
    orch.close()
    print("\ndatabase retained at", db_path)


if __name__ == "__main__":
    main()
