"""NPZ stage artifacts with embedded metadata and content fingerprints."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import (
    array_fingerprint,
    canonical_json,
    file_sha256,
    fsync_directory,
    hash_object,
    normalized_array,
)


ARTIFACT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Artifact:
    path: Path
    data: np.ndarray
    meta: dict[str, Any]
    file_sha256: str

    @property
    def content_fingerprint(self) -> str:
        return str(self.meta["content_fingerprint"])


def make_content_fingerprint(
    *,
    stage: str,
    kind: str,
    parameter_fingerprint: str,
    dependency_fingerprints: dict[str, str],
    data: np.ndarray,
) -> str:
    return hash_object(
        {
            "artifact_format_version": ARTIFACT_FORMAT_VERSION,
            "stage": stage,
            "kind": kind,
            "parameter_fingerprint": parameter_fingerprint,
            "dependency_fingerprints": dependency_fingerprints,
            "data_sha256": array_fingerprint(data),
            "shape": list(np.asarray(data).shape),
            "dtype": normalized_array(data).dtype.str,
        }
    )


def save_artifact(
    path: Path | str,
    *,
    stage: str,
    kind: str,
    artifact_version: int,
    parameter_fingerprint: str,
    dependency_fingerprints: dict[str, str],
    data: np.ndarray,
) -> Artifact:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    canonical_data = normalized_array(data)
    content_fingerprint = make_content_fingerprint(
        stage=stage,
        kind=kind,
        parameter_fingerprint=parameter_fingerprint,
        dependency_fingerprints=dependency_fingerprints,
        data=canonical_data,
    )
    meta: dict[str, Any] = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "stage": stage,
        "kind": kind,
        "artifact_version": artifact_version,
        "content_fingerprint": content_fingerprint,
        "parameter_fingerprint": parameter_fingerprint,
        "dependency_fingerprints": dependency_fingerprints,
        "shape": list(canonical_data.shape),
        "dtype": canonical_data.dtype.str,
        "data_sha256": array_fingerprint(canonical_data),
    }
    temp = target.with_name(
        f".{target.stem}.tmp-{os.getpid()}-{id(parameter_fingerprint)}.npz"
    )
    try:
        with open(temp, "wb") as stream:
            np.savez_compressed(
                stream,
                data=canonical_data,
                meta=np.asarray(canonical_json(meta)),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return Artifact(
        path=target,
        data=canonical_data,
        meta=meta,
        file_sha256=file_sha256(target),
    )


def read_artifact(path: Path | str, *, validate: bool = True) -> Artifact:
    target = Path(path)
    try:
        with np.load(target, allow_pickle=False) as archive:
            if "data" not in archive or "meta" not in archive:
                raise ValueError("artifact must contain data and meta arrays")
            data = np.asarray(archive["data"]).copy()
            meta = json.loads(str(archive["meta"].item()))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid artifact {target}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError(f"invalid artifact metadata in {target}")
    if validate:
        required = {
            "artifact_format_version",
            "stage",
            "kind",
            "artifact_version",
            "content_fingerprint",
            "parameter_fingerprint",
            "dependency_fingerprints",
            "shape",
            "dtype",
            "data_sha256",
        }
        missing = required.difference(meta)
        if missing:
            raise ValueError(f"artifact metadata missing keys: {sorted(missing)}")
        canonical_data = normalized_array(data)
        if list(canonical_data.shape) != meta["shape"]:
            raise ValueError("artifact shape does not match metadata")
        if canonical_data.dtype.str != meta["dtype"]:
            raise ValueError("artifact dtype does not match metadata")
        if array_fingerprint(canonical_data) != meta["data_sha256"]:
            raise ValueError("artifact data fingerprint does not match metadata")
        expected = make_content_fingerprint(
            stage=str(meta["stage"]),
            kind=str(meta["kind"]),
            parameter_fingerprint=str(meta["parameter_fingerprint"]),
            dependency_fingerprints=dict(meta["dependency_fingerprints"]),
            data=canonical_data,
        )
        if expected != meta["content_fingerprint"]:
            raise ValueError("artifact content fingerprint does not match metadata")
        data = canonical_data
    return Artifact(path=target, data=data, meta=meta, file_sha256=file_sha256(target))
