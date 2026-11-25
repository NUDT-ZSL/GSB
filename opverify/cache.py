from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .hashing import atomic_write_bytes, canonical_bytes, read_json, sha256_bytes, sha256_json


class ArtifactCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.entries = self.root / "keys"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.entries.mkdir(parents=True, exist_ok=True)

    def blob_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest[2:4] / digest

    def put_bytes(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        path = self.blob_path(digest)
        if not path.exists():
            atomic_write_bytes(path, data)
        return digest

    def get_bytes(self, digest: str) -> bytes:
        path = self.blob_path(digest)
        if not path.exists():
            from .errors import OpVerifyError

            raise OpVerifyError(
                "E_ARTIFACT_MISSING",
                f"content-addressed artifact is missing: {digest}",
                details={"digest": digest},
            )
        return path.read_bytes()

    def put_json(self, value: Any) -> str:
        from .hashing import canonical_bytes

        return self.put_bytes(canonical_bytes(value))

    def get_json(self, digest: str) -> Any:
        import json

        return json.loads(self.get_bytes(digest).decode("utf-8"))

    def has_blob(self, digest: str) -> bool:
        return self.blob_path(digest).exists()

    def put_array_artifact(self, array: np.ndarray, dtype: str = "float64") -> tuple[str, dict[str, Any]]:
        canonical = np.ascontiguousarray(array).astype(np.dtype("<f8"), copy=False)
        byte_digest = self.put_bytes(canonical.tobytes(order="C"))
        manifest = {
            "artifact_type": "opverify-float-array",
            "logical_dtype": dtype,
            "canonical_dtype": "float64-le",
            "shape": [int(item) for item in array.shape],
            "byte_digest": byte_digest,
        }
        artifact_digest = self.put_json(manifest)
        return artifact_digest, manifest

    def get_array_artifact(self, artifact_digest: str) -> np.ndarray:
        manifest = self.get_json(artifact_digest)
        data = self.get_bytes(manifest["byte_digest"])
        return np.frombuffer(data, dtype="<f8").reshape(tuple(manifest["shape"])).copy()

    def entry_path(self, cache_key: str) -> Path:
        return self.entries / f"{cache_key}.json"

    def get_entry(self, cache_key: str) -> dict[str, Any] | None:
        path = self.entry_path(cache_key)
        if not path.exists():
            return None
        entry = read_json(path)
        if not self.has_blob(entry["output_digest"]):
            return None
        return entry

    def put_entry(self, cache_key: str, entry: dict[str, Any]) -> None:
        atomic_write_bytes(self.entry_path(cache_key), canonical_bytes(entry))


def build_cache_key(
    task: dict[str, Any],
    *,
    input_hash: str,
    parameter_hash: str,
    upstream_artifacts: list[dict[str, str]] | None = None,
    weight_hash: str | None = None,
) -> tuple[str, dict[str, Any]]:
    upstream = sorted(
        (
            {
                "job_id": str(item["job_id"]),
                "artifact_hash": str(item["artifact_hash"]),
            }
            for item in (upstream_artifacts or [])
        ),
        key=lambda item: item["job_id"],
    )
    payload = {
        "schema": "opverify-cache-key-v1",
        "op_kind": task["op_kind"],
        "interp_mode": task.get("interp_mode"),
        "input_hash": input_hash,
        "weight_hash": weight_hash,
        "parameter_digest": parameter_hash,
        "block_policy": task["block_policy"],
        "revision": int(task["revision"]),
        "upstream_artifact_hashes": upstream,
    }
    return sha256_json(payload), payload
