"""Content-addressed cache with auditable key composition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .canonical import atomic_write_json, canonical_bytes, sha256_value
from .errors import BusinessError
from .models import Session, SummaryConfig, Vocabulary


def chunk_scope_hash(session: Session, end_turn_index: int) -> str:
    """Hash the immutable session prefix consumed by a chunk.

    Scoping the session content hash to the consumed prefix is what makes an
    append operation reusable for already-complete blocks. Appending to an
    incomplete final block changes that block's prefix and therefore its key.
    """

    return sha256_value(
        {
            "session_id": session.session_id,
            "content_scope": "turn_prefix",
            "end_turn_index": end_turn_index,
            "turns": [
                turn.to_dict()
                for turn in session.turns
                if turn.turn_index <= end_turn_index
            ],
            "units": [
                unit.to_dict()
                for unit in session.units
                if unit.turn_index <= end_turn_index
            ],
            "rewrite_constraints": session.constraints.to_dict(),
        }
    )


def unit_scope_hash(session: Session, unit_id: str) -> str:
    unit = next(item for item in session.units if item.unit_id == unit_id)
    return sha256_value(
        {
            "session_id": session.session_id,
            "content_scope": "unit",
            "unit": unit.to_dict(),
            "rewrite_constraints": session.constraints.to_dict(),
        }
    )


def chunk_key_components(
    session: Session,
    vocabulary: Vocabulary,
    config: SummaryConfig,
    chunk: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "node_type": "chunk_tokenize",
        "session_id": session.session_id,
        "session_content_hash": chunk_scope_hash(
            session, int(chunk["end_turn_index"])
        ),
        "session_content_hash_scope": "turn_prefix",
        "vocab_version": vocabulary.vocab_version,
        "vocab_content_hash": vocabulary.content_hash,
        "precision": config.precision,
        "rewrite_strength": None,
        "smoothing": None,
        "upstream_hash": None,
        "block_index": int(chunk["block_index"]),
        "turn_indices": list(chunk["turn_indices"]),
        "input_content_hash": sha256_value(dict(chunk)),
    }


def score_key_components(
    session: Session,
    vocabulary: Vocabulary,
    config: SummaryConfig,
    chunk_artifact: Mapping[str, Any],
    scored_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "node_type": "unit_score",
        "session_id": session.session_id,
        "session_content_hash": sha256_value(
            {
                "content_scope": "block_units",
                "block_index": int(scored_payload["block_index"]),
                "units": [
                    unit.to_dict()
                    for unit in session.units
                    if int(scored_payload["start_turn_index"])
                    <= unit.turn_index
                    <= int(scored_payload["end_turn_index"])
                ],
            }
        ),
        "session_content_hash_scope": "block_units",
        "vocab_version": vocabulary.vocab_version,
        "vocab_content_hash": vocabulary.content_hash,
        "precision": config.precision,
        "rewrite_strength": None,
        "smoothing": config.smoothing,
        "upstream_hash": chunk_artifact["content_hash"],
        "weights": {name: float(config.weights[name]) for name in config.weights},
        "block_index": int(scored_payload["block_index"]),
    }


def rewrite_key_components(
    session: Session,
    vocabulary: Vocabulary,
    config: SummaryConfig,
    score_artifact: Mapping[str, Any],
    rewrite_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "node_type": "rewrite",
        "session_id": session.session_id,
        "session_content_hash": unit_scope_hash(
            session, str(rewrite_payload["unit_id"])
        ),
        "session_content_hash_scope": "unit",
        "vocab_version": vocabulary.vocab_version,
        "vocab_content_hash": vocabulary.content_hash,
        "precision": config.precision,
        "rewrite_strength": config.rewrite_strength,
        "smoothing": config.smoothing,
        "upstream_hash": score_artifact["content_hash"],
        "score_content_hash": rewrite_payload["score_content_hash"],
        "constraints_hash": rewrite_payload["constraints_hash"],
        "unit_id": rewrite_payload["unit_id"],
    }


def merge_block_key_components(
    session: Session,
    config: SummaryConfig,
    merge_payload: Mapping[str, Any],
    rewrite_artifact_hashes: Sequence[str],
    vocabulary: Vocabulary,
) -> Dict[str, Any]:
    upstream = sha256_value(list(rewrite_artifact_hashes))
    source_hash = sha256_value(
        {
            "source_token_count": merge_payload["source_token_count"],
            "source_terms": merge_payload["source_terms"],
        }
    )
    return {
        "node_type": "summary_merge",
        "merge_scope": "block",
        "session_id": session.session_id,
        "session_content_hash": sha256_value(
            {
                "content_scope": "merge_block",
                "block_index": merge_payload["block_index"],
                "rewrite_hashes": list(rewrite_artifact_hashes),
                "source_hash": source_hash,
            }
        ),
        "session_content_hash_scope": "merge_block",
        "vocab_version": vocabulary.vocab_version,
        "vocab_content_hash": vocabulary.content_hash,
        "precision": config.precision,
        "rewrite_strength": config.rewrite_strength,
        "smoothing": config.smoothing,
        "upstream_hash": upstream,
        "upstream_hashes": list(rewrite_artifact_hashes),
        "source_hash": source_hash,
        "block_index": int(merge_payload["block_index"]),
    }


def merge_final_key_components(
    session: Session,
    config: SummaryConfig,
    block_artifact_hashes: Sequence[str],
    vocabulary: Vocabulary,
) -> Dict[str, Any]:
    return {
        "node_type": "summary_merge",
        "merge_scope": "final",
        "session_id": session.session_id,
        "session_content_hash": sha256_value(
            {
                "content_scope": "final_merge",
                "block_hashes": list(block_artifact_hashes),
            }
        ),
        "session_content_hash_scope": "final_merge",
        "vocab_version": vocabulary.vocab_version,
        "vocab_content_hash": vocabulary.content_hash,
        "precision": config.precision,
        "rewrite_strength": config.rewrite_strength,
        "smoothing": config.smoothing,
        "upstream_hash": sha256_value(list(block_artifact_hashes)),
        "upstream_hashes": list(block_artifact_hashes),
    }


class CacheStore:
    """A local filesystem object store keyed by canonical key components."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.events: List[Dict[str, Any]] = []
        self.stats: Dict[str, int] = {"hit": 0, "miss": 0}

    def _path_for(self, key: str) -> Path:
        return self.objects_dir / key[:2] / (key + ".json")

    def make_key(self, key_components: Mapping[str, Any]) -> str:
        return sha256_value(dict(key_components))

    def get_or_compute(
        self,
        node_type: str,
        session_id: str,
        key_components: Mapping[str, Any],
        compute: Callable[[], Mapping[str, Any]],
        *,
        node_id: Optional[str] = None,
        bypass: bool = False,
    ) -> Tuple[Dict[str, Any], str]:
        key = self.make_key(key_components)
        path = self._path_for(key)
        if not bypass and path.exists():
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
                payload = artifact["payload"]
                content_hash = artifact["content_hash"]
                if (
                    artifact.get("cache_key") != key
                    or artifact.get("node_type") != node_type
                    or artifact.get("session_id") != session_id
                    or sha256_value(artifact.get("key_components", {})) != key
                    or sha256_value(payload) != content_hash
                ):
                    raise ValueError("cache envelope consistency failure")
            except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    f"cache object {key} is corrupt: {exc}",
                    session_id=session_id,
                    details={"cache_key": key},
                ) from exc
            self.stats["hit"] += 1
            self.events.append(
                {
                    "stage": node_type,
                    "node_id": node_id or f"{session_id}/{node_type}",
                    "session_id": session_id,
                    "status": "reused",
                    "cache_key": key,
                    "content_hash": content_hash,
                }
            )
            return artifact, "reused"

        payload = dict(compute())
        content_hash = sha256_value(payload)
        artifact = {
            "schema_version": 1,
            "node_type": node_type,
            "session_id": session_id,
            "cache_key": key,
            "key_components": dict(key_components),
            "content_hash": content_hash,
            "payload": payload,
        }
        atomic_write_json(path, artifact)
        self.stats["miss"] += 1
        self.events.append(
            {
                "stage": node_type,
                "node_id": node_id or f"{session_id}/{node_type}",
                "session_id": session_id,
                "status": "recomputed",
                "cache_key": key,
                "content_hash": content_hash,
            }
        )
        return artifact, "recomputed"

    def query_session(self, session_id: str) -> List[Dict[str, Any]]:
        matches: List[Dict[str, Any]] = []
        for path in sorted(self.objects_dir.glob("*/*.json")):
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if artifact.get("session_id") != session_id:
                continue
            matches.append(
                {
                    "node_type": artifact.get("node_type"),
                    "cache_key": artifact.get("cache_key"),
                    "content_hash": artifact.get("content_hash"),
                    "key_components": artifact.get("key_components", {}),
                }
            )
        matches.sort(
            key=lambda item: (
                str(item["node_type"]),
                str(item["key_components"].get("block_index", -1)),
                str(item["key_components"].get("unit_id", "")),
                str(item["cache_key"]),
            )
        )
        return matches

    def object_bytes(self, cache_key: str) -> bytes:
        path = self._path_for(cache_key)
        if not path.exists():
            raise BusinessError(
                "E_CACHE_NOT_FOUND",
                f"cache object {cache_key} does not exist",
                details={"cache_key": cache_key},
            )
        return canonical_bytes(json.loads(path.read_text(encoding="utf-8")))
