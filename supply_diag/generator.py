"""Local deterministic manifest generator for offline validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
from pathlib import Path
from typing import Any

from .report import atomic_write_json
from .truth import compute_truth_marker, truth_marker_payload


GENERATOR_VERSION = "1.0.0"
DEFAULT_SEED = 20261118


def _content_bytes(content: Any) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    return json.dumps(
        content,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_content(algorithm: str, content: Any, key: str) -> str:
    payload = _content_bytes(content)
    if algorithm == "sha256":
        return hashlib.sha256(payload).hexdigest()
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _truth(
    *,
    project_id: str,
    component_id: str,
    version_components: list[int],
    algorithm: str,
    expected_cumulative_width: str,
) -> dict:
    expected_bound = {
        "lower": expected_cumulative_width,
        "upper": expected_cumulative_width,
        "unit": "quantum",
        "scope": "cumulative_version_width",
    }
    payload = truth_marker_payload(
        project_id=project_id,
        component_id=component_id,
        raw_version_components=version_components,
        declared_algorithm=algorithm,
        expected_error_bound=expected_bound,
    )
    return {
        "marker_version": "1",
        "raw_version_components": version_components,
        "declared_algorithm": algorithm,
        "expected_error_bound": expected_bound,
        "sync_marker": compute_truth_marker(payload),
    }


def _component(
    *,
    project_id: str,
    component_id: str,
    version: str,
    signature_status: str,
    algorithm: str,
    dependencies: list[dict] | None,
    expected_cumulative_width: str,
    content_nonce: str,
    hmac_key: str | None = None,
    corrupt_hash: bool = False,
) -> dict:
    content = {
        "project": project_id,
        "component": component_id,
        "version": version,
        "nonce": content_nonce,
        "artifacts": [f"{component_id}.dll", f"{component_id}.pdb"],
    }
    digest = _hash_content(algorithm, content, hmac_key or f"{project_id}:{component_id}")
    if corrupt_hash:
        digest = "0" * 64
    result = {
        "component_id": component_id,
        "version": version,
        "hash_algorithm": algorithm,
        "content_hash": digest,
        "signature_status": signature_status,
        "dependencies": dependencies or [],
        "content": content,
        "truth": _truth(
            project_id=project_id,
            component_id=component_id,
            version_components=[int(part) for part in version.split(".")],
            algorithm=algorithm,
            expected_cumulative_width=expected_cumulative_width,
        ),
    }
    if algorithm == "hmac-sha256":
        result["hmac_key"] = hmac_key
    return result


def _alpha(seed: int) -> dict:
    project = "alpha-suite"
    nonce = f"alpha-{seed}"
    components = [
        _component(
            project_id=project,
            component_id="app",
            version="3.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "core", "constraint": "[1.2.0,2.0.0)"},
                {"target": "shared", "constraint": "1.0.0 - 2.0.0"},
            ],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="core",
            version="1.5.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "shared", "constraint": "(1.4.0,2.0.0]"},
                {"target": "crypto", "constraint": ">=1.0.0, <=1.0.2"},
            ],
            expected_cumulative_width="2",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="shared",
            version="1.8.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "crypto", "constraint": ">=1.0.0 <1.2.0"},
            ],
            expected_cumulative_width="3",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="crypto",
            version="1.0.1",
            signature_status="unsigned",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="4",
            content_nonce=nonce,
        ),
    ]
    return {
        "project_id": project,
        "manifest_version": "1.0.0",
        "components": components,
    }


def _beta(seed: int) -> dict:
    project = "beta-edge"
    nonce = f"beta-{seed}"
    hmac_key = f"{project}:hmac-key"
    components = [
        _component(
            project_id=project,
            component_id="bundle",
            version="5.0.0",
            signature_status="signed",
            algorithm="hmac-sha256",
            dependencies=[
                {"target": "border", "constraint": "<=1.0.0"},
                {"target": "far", "constraint": ">=99.0.0"},
            ],
            expected_cumulative_width="1",
            content_nonce=nonce,
            hmac_key=hmac_key,
        ),
        _component(
            project_id=project,
            component_id="alt",
            version="2.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "border", "constraint": ">1.0.0"},
            ],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="border",
            version="1.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="3",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="far",
            version="100.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="2",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="hash-bad",
            version="1.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="1",
            content_nonce=nonce,
            corrupt_hash=True,
        ),
        _component(
            project_id=project,
            component_id="unknown-sig",
            version="1.0.0",
            signature_status="unknown",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="conflict-a",
            version="1.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "conflict-target", "constraint": "<=1.0.0"},
            ],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="conflict-b",
            version="1.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "conflict-target", "constraint": ">=3.0.0"},
            ],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="conflict-target",
            version="2.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="2",
            content_nonce=nonce,
        ),
    ]
    return {
        "project_id": project,
        "manifest_version": "2.1.0",
        "components": components,
    }


def _gamma(seed: int) -> dict:
    project = "gamma-invalid"
    nonce = f"gamma-{seed}"
    duplicate_one = _component(
        project_id=project,
        component_id="dup",
        version="1.0.0",
        signature_status="signed",
        algorithm="sha256",
        dependencies=[],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    duplicate_two = json.loads(json.dumps(duplicate_one))
    bad_algorithm = _component(
        project_id=project,
        component_id="bad-algo",
        version="1.0.0",
        signature_status="signed",
        algorithm="sha256",
        dependencies=[],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    bad_algorithm["hash_algorithm"] = "md5"
    bad_signature = _component(
        project_id=project,
        component_id="bad-signature",
        version="1.0.0",
        signature_status="signed",
        algorithm="sha256",
        dependencies=[],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    bad_signature["signature_status"] = "maybe"
    orphan = _component(
        project_id=project,
        component_id="orphan",
        version="1.0.0",
        signature_status="signed",
        algorithm="sha256",
        dependencies=[{"target": "absent", "constraint": ">=1.0.0"}],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    cycle_a = _component(
        project_id=project,
        component_id="cycle-a",
        version="1.0.0",
        signature_status="unsigned",
        algorithm="sha256",
        dependencies=[{"target": "cycle-b", "constraint": "[1.0.0,1.0.0]"}],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    cycle_b = _component(
        project_id=project,
        component_id="cycle-b",
        version="1.0.0",
        signature_status="signed",
        algorithm="sha256",
        dependencies=[{"target": "cycle-a", "constraint": "1.0.0"}],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    self_loop = _component(
        project_id=project,
        component_id="self-loop",
        version="1.0.0",
        signature_status="signed",
        algorithm="sha256",
        dependencies=[{"target": "self-loop", "constraint": "1.0.0"}],
        expected_cumulative_width="1",
        content_nonce=nonce,
    )
    return {
        "project_id": project,
        "manifest_version": "1.0.1",
        "components": [
            duplicate_one,
            duplicate_two,
            bad_algorithm,
            bad_signature,
            orphan,
            cycle_a,
            cycle_b,
            self_loop,
        ],
    }


def _delta(seed: int) -> dict:
    project = "delta-secure"
    nonce = f"delta-{seed}"
    hmac_key = f"{project}:library-key"
    components = [
        _component(
            project_id=project,
            component_id="service",
            version="4.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[
                {"target": "library", "constraint": "[2.0.0,3.0.0)"},
            ],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
        _component(
            project_id=project,
            component_id="library",
            version="2.4.0",
            signature_status="signed",
            algorithm="hmac-sha256",
            dependencies=[],
            expected_cumulative_width="2",
            content_nonce=nonce,
            hmac_key=hmac_key,
        ),
        _component(
            project_id=project,
            component_id="policy",
            version="1.0.0",
            signature_status="signed",
            algorithm="sha256",
            dependencies=[],
            expected_cumulative_width="1",
            content_nonce=nonce,
        ),
    ]
    return {
        "project_id": project,
        "manifest_version": "3.2.1",
        "components": components,
    }


def generate_samples(output_dir: str | Path, *, seed: int = DEFAULT_SEED) -> dict:
    """Write a deterministic sample set and return its metadata."""

    root = Path(output_dir)
    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    manifest_specs = [
        ("alpha-suite.json", _alpha(seed)),
        ("beta-edge.json", _beta(seed)),
        ("gamma-invalid.json", _gamma(seed)),
        ("delta-secure.json", _delta(seed)),
    ]
    # A deterministic extra nonce variation keeps repeated generation stable
    # while making it inconvenient to hard-code report digests.
    rng_nonce = rng.getrandbits(32)
    files = []
    for filename, manifest in manifest_specs:
        path = manifests_dir / filename
        atomic_write_json(path, manifest)
        raw = path.read_bytes()
        files.append(
            {
                "name": filename,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    metadata = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "nonce_fingerprint": f"{rng_nonce:08x}",
        "manifest_directory": "manifests",
        "files": sorted(files, key=lambda item: item["name"]),
        "deterministic": True,
    }
    atomic_write_json(root / "generator_metadata.json", metadata)
    return metadata

