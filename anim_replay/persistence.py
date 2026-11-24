from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_json, sha256_value, verify_digest, with_digest
from .concurrency import precompute_clip_cache
from .engine import (
    build_chunk_dependencies,
    chunk_bounds,
    chunk_count,
    chunk_times,
    simulate_frames,
)
from .errors import DigestError, SchemaError, VersionError
from .math3d import is_finite_number
from .models import (
    CHANNELS,
    ChunkResult,
    MachineState,
    ReplayFrame,
    ReplayResult,
    Spec,
    TrackSample,
    TransitionEvent,
)
from .schema import version_id


CHUNK_FORMAT = "anim-replay-chunk-v1"
MANIFEST_FORMAT = "anim-replay-state-manifest-v1"


@dataclass
class StopToken:
    requested: bool = False
    exit_code: int = 130
    signal_name: str | None = None
    reason: str | None = None

    def request(self, exit_code: int = 130, *, signal_name: str | None = None, reason: str | None = None) -> None:
        if not self.requested:
            self.requested = True
            self.exit_code = int(exit_code)
            self.signal_name = signal_name
            self.reason = reason


@dataclass
class ResumeState:
    chunks: list[ChunkResult]
    next_chunk: int
    state: MachineState
    invalid_chunks: list[dict[str, Any]] = field(default_factory=list)
    manifest_present: bool = False
    recovered_frame_count: int = 0
    recovered_event_count: int = 0


@dataclass
class PersistenceResult:
    result: ReplayResult
    report: dict[str, Any]
    interrupted: bool = False
    exit_code: int = 0


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _chunk_path(state_dir: Path, index: int) -> Path:
    return state_dir / "chunks" / f"chunk_{index:08d}.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError("cannot read persisted state JSON", location=str(path), details={"error": str(exc)}) from exc


def _manifest_body(
    spec: Spec,
    version: str,
    *,
    last_committed: int,
    frame_count: int,
    event_count: int,
) -> dict[str, Any]:
    return {
        "format": MANIFEST_FORMAT,
        "version_id": version,
        "frame_step_ms": spec.timeline.frame_step_ms,
        "chunk_ms": spec.timeline.chunk_ms,
        "duration_ms": spec.timeline.duration_ms,
        "expected_chunk_count": chunk_count(spec),
        "last_committed_chunk": last_committed,
        "frame_count": frame_count,
        "event_count": event_count,
    }


def _check_state_version(state_dir: Path, expected_version: str, spec: Spec) -> None:
    manifest_path = state_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = _read_json(manifest_path)
        except SchemaError:
            return
        existing_version = manifest.get("version_id")
        if existing_version is not None and existing_version != expected_version:
            raise VersionError(
                "state directory belongs to a different normalized animation version",
                location=str(manifest_path),
                details={"existing": existing_version, "expected": expected_version},
            )
        if manifest.get("chunk_ms") not in (None, spec.timeline.chunk_ms):
            raise VersionError(
                "state directory uses a different chunk size",
                location=str(manifest_path),
                details={"existing": manifest.get("chunk_ms"), "expected": spec.timeline.chunk_ms},
            )
        if manifest.get("frame_step_ms") not in (None, spec.timeline.frame_step_ms):
            raise VersionError(
                "state directory uses a different frame step",
                location=str(manifest_path),
                details={
                    "existing": manifest.get("frame_step_ms"),
                    "expected": spec.timeline.frame_step_ms,
                },
            )
        if manifest.get("duration_ms") not in (None, spec.timeline.duration_ms):
            raise VersionError(
                "state directory uses a different timeline duration",
                location=str(manifest_path),
                details={
                    "existing": manifest.get("duration_ms"),
                    "expected": spec.timeline.duration_ms,
                },
            )
        return
    chunks_dir = state_dir / "chunks"
    if chunks_dir.exists():
        for path in sorted(chunks_dir.glob("chunk_*.json")):
            try:
                document = _read_json(path)
            except SchemaError:
                continue
            existing_version = document.get("version_id")
            if existing_version is not None and existing_version != expected_version:
                raise VersionError(
                    "existing chunk belongs to a different normalized animation version",
                    location=str(path),
                    details={"existing": existing_version, "expected": expected_version},
                )
            break


def _check_replace_version(path: Path, expected_version: str) -> None:
    if not path.exists():
        return
    try:
        document = _read_json(path)
    except SchemaError:
        return
    existing_version = document.get("version_id")
    if existing_version is not None and existing_version != expected_version:
        raise VersionError(
            "refusing to replace a chunk from a different normalized animation version",
            location=str(path),
            details={"existing": existing_version, "expected": expected_version},
        )


def _parse_transform(flat: Mapping[str, Any], bone: str) -> tuple[tuple[float, float, float], tuple[float, float, float, float], tuple[float, float, float]]:
    values: dict[str, list[float]] = {}
    for channel in CHANNELS:
        key = f"{bone}.{channel}"
        raw = flat.get(key)
        expected = 4 if channel == "rotation" else 3
        if not isinstance(raw, list) or len(raw) != expected or not all(is_finite_number(item) for item in raw):
            raise DigestError(
                "persisted frame is missing a valid bone channel",
                details={"bone_channel": key},
            )
        values[channel] = [float(item) for item in raw]
    return (
        (values["translation"][0], values["translation"][1], values["translation"][2]),
        (values["rotation"][0], values["rotation"][1], values["rotation"][2], values["rotation"][3]),
        (values["scale"][0], values["scale"][1], values["scale"][2]),
    )


def _frame_from_document(
    document: Mapping[str, Any],
    *,
    expected_version: str,
    expected_index: int,
    bone_names: Sequence[str],
) -> ReplayFrame:
    bones = document.get("bones")
    if not isinstance(bones, dict):
        raise DigestError("persisted frame bones are malformed", details={"chunk": expected_index})
    world = {bone: _parse_transform(bones, bone) for bone in bone_names}
    dependencies = document.get("dependencies", {})
    samples: list[TrackSample] = []
    if isinstance(dependencies, dict):
        for item in dependencies.get("tracks", []):
            if not isinstance(item, dict):
                continue
            try:
                samples.append(
                    TrackSample(
                        bone=str(item["bone"]),
                        channel=str(item["channel"]),
                        lo_ms=int(item["lo_ms"]),
                        hi_ms=int(item["hi_ms"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DigestError("persisted frame dependencies are malformed", details={"chunk": expected_index}) from exc
    try:
        return ReplayFrame(
            t=int(document["t"]),
            clip=str(document["clip"]),
            local_ms=int(document["local_ms"]),
            generation=int(document["state_generation"]),
            world=world,
            track_samples=tuple(samples),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DigestError("persisted frame is malformed", details={"chunk": expected_index}) from exc


def _event_from_document(document: Mapping[str, Any]) -> TransitionEvent:
    try:
        if document.get("type") != "transition":
            raise ValueError("not a transition")
        return TransitionEvent(
            t=int(document["t"]),
            rule_id=str(document["rule_id"]),
            source_clip=str(document["from"]),
            target_clip=str(document["to"]),
            priority=int(document["priority"]),
            state_generation=int(document["state_generation"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DigestError("persisted transition event is malformed") from exc


def _chunk_from_document(
    document: Mapping[str, Any],
    *,
    expected_version: str,
    expected_index: int,
    bone_names: Sequence[str],
) -> ChunkResult:
    if document.get("format") != CHUNK_FORMAT:
        raise DigestError("persisted chunk format is unsupported", details={"chunk": expected_index})
    if document.get("version_id") != expected_version:
        raise VersionError(
            "persisted chunk version does not match the active input",
            details={"chunk": expected_index, "version_id": document.get("version_id")},
        )
    if int(document.get("chunk_index", -1)) != expected_index:
        raise DigestError("persisted chunk index is malformed", details={"chunk": expected_index})
    try:
        verify_digest(document, location=f"chunk[{expected_index}]")
    except DigestError:
        raise
    frames_raw = document.get("frames")
    events_raw = document.get("events")
    if not isinstance(frames_raw, list) or not isinstance(events_raw, list):
        raise DigestError("persisted chunk frames/events are malformed", details={"chunk": expected_index})
    if int(document.get("frame_count", -1)) != len(frames_raw):
        raise DigestError("persisted chunk frame count mismatch", details={"chunk": expected_index})
    frames = [
        _frame_from_document(
            frame,
            expected_version=expected_version,
            expected_index=expected_index,
            bone_names=bone_names,
        )
        for frame in frames_raw
        if isinstance(frame, dict)
    ]
    if len(frames) != len(frames_raw):
        raise DigestError("persisted frame is not an object", details={"chunk": expected_index})
    events = [
        _event_from_document(event)
        for event in events_raw
        if isinstance(event, dict)
    ]
    if len(events) != len(events_raw):
        raise DigestError("persisted event is not an object", details={"chunk": expected_index})
    try:
        state_at_start = MachineState.from_dict(document["state_at_start"])
        state_at_end = MachineState.from_dict(document["state_at_end"])
        start_ms = int(document["logical_start_ms"])
        end_ms = int(document["logical_end_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DigestError("persisted chunk state is malformed", details={"chunk": expected_index}) from exc
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, dict):
        raise DigestError("persisted chunk dependencies are malformed", details={"chunk": expected_index})
    return ChunkResult(
        index=expected_index,
        start_ms=start_ms,
        end_ms=end_ms,
        frames=frames,
        events=events,
        state_at_start=state_at_start,
        state_at_end=state_at_end,
        dependencies=dict(dependencies),
        source="recovered",
    )


def recover_state(
    state_dir: str | Path,
    spec: Spec,
    *,
    expected_version: str | None = None,
) -> ResumeState:
    directory = Path(state_dir)
    version = expected_version or version_id(spec)
    _check_state_version(directory, version, spec)
    initial_clip = spec.clips[spec.initial_clip]
    default_state = MachineState(
        active_clip=spec.initial_clip,
        enter_ms=0,
        target_loop_count=initial_clip.loop_count,
        generation=0,
    )
    chunks: list[ChunkResult] = []
    invalid: list[dict[str, Any]] = []
    manifest_present = (directory / "manifest.json").exists()
    bone_names = [bone.name for bone in spec.skeleton]
    for index in range(chunk_count(spec)):
        path = _chunk_path(directory, index)
        if not path.exists():
            later = sorted((directory / "chunks").glob("chunk_*.json")) if (directory / "chunks").exists() else []
            if any(item.name > path.name for item in later):
                invalid.append({"chunk": index, "reason": "gap_before_later_chunks"})
            break
        try:
            document = _read_json(path)
            chunk = _chunk_from_document(
                document,
                expected_version=version,
                expected_index=index,
                bone_names=bone_names,
            )
            if chunks:
                if chunks[-1].state_at_end != chunk.state_at_start:
                    raise DigestError(
                        "persisted chunk boundary state is discontinuous",
                        details={"chunk": index},
                    )
            elif chunk.state_at_start != default_state:
                raise DigestError(
                    "first persisted chunk does not start from the initial state",
                    details={"chunk": index},
                )
        except VersionError:
            raise
        except (DigestError, SchemaError) as exc:
            invalid.append(
                {
                    "chunk": index,
                    "reason": "digest_or_schema_invalid",
                    "message": exc.message,
                }
            )
            break
        chunks.append(chunk)
    state = chunks[-1].state_at_end if chunks else default_state
    return ResumeState(
        chunks=chunks,
        next_chunk=len(chunks),
        state=state,
        invalid_chunks=invalid,
        manifest_present=manifest_present,
        recovered_frame_count=sum(len(chunk.frames) for chunk in chunks),
        recovered_event_count=sum(len(chunk.events) for chunk in chunks),
    )


def _write_manifest(
    state_dir: Path,
    spec: Spec,
    version: str,
    chunks: Sequence[ChunkResult],
) -> None:
    body = _manifest_body(
        spec,
        version,
        last_committed=len(chunks) - 1,
        frame_count=sum(len(chunk.frames) for chunk in chunks),
        event_count=sum(len(chunk.events) for chunk in chunks),
    )
    _atomic_write_json(state_dir / "manifest.json", with_digest(body))


def persist_chunk(
    state_dir: str | Path,
    chunk: ChunkResult,
    version: str,
) -> None:
    directory = Path(state_dir)
    path = _chunk_path(directory, chunk.index)
    _check_replace_version(path, version)
    document = with_digest(chunk.content_dict(version))
    _atomic_write_json(path, document)


def persist_replay_result(
    result: ReplayResult,
    state_dir: str | Path,
    *,
    stop_token: StopToken | None = None,
) -> None:
    directory = Path(state_dir)
    _check_state_version(directory, result.version_id, result.spec)
    for chunk in result.chunks:
        if stop_token is not None and stop_token.requested:
            break
        persist_chunk(directory, chunk, result.version_id)
        _write_manifest(directory, result.spec, result.version_id, result.chunks[: chunk.index + 1])
    if stop_token is None or not stop_token.requested:
        _write_manifest(directory, result.spec, result.version_id, result.chunks)
        _write_final_report(directory, result, extra={"mode": "persist"})


def _write_final_report(
    state_dir: Path,
    result: ReplayResult,
    *,
    extra: Mapping[str, Any] | None = None,
) -> None:
    payload = {
        "ok": True,
        "version_id": result.version_id,
        "chunk_count": len(result.chunks),
        "frame_count": len(result.frames),
        "event_count": len(result.events),
        "content_digest": output_digest(result),
        "concurrency": result.concurrency,
    }
    if extra:
        payload.update(dict(extra))
    _atomic_write_json(state_dir / "report.json", payload)


def run_persistent_replay(
    spec: Spec,
    state_dir: str | Path,
    *,
    max_workers: int = 1,
    memory_budget_bytes: int = 64 * 1024 * 1024,
    stop_token: StopToken | None = None,
    stop_after_chunks: int | None = None,
    resume: bool = True,
) -> PersistenceResult:
    directory = Path(state_dir)
    token = stop_token or StopToken()
    version = version_id(spec)
    _check_state_version(directory, version, spec)
    recovered = (
        recover_state(directory, spec, expected_version=version)
        if resume
        else ResumeState(
            chunks=[],
            next_chunk=0,
            state=MachineState(
                active_clip=spec.initial_clip,
                enter_ms=0,
                target_loop_count=spec.clips[spec.initial_clip].loop_count,
                generation=0,
            ),
        )
    )
    cache, concurrency_report = precompute_clip_cache(
        spec,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )
    chunks = list(recovered.chunks)
    state = recovered.state
    written: list[int] = []
    if token.requested:
        interrupted = True
    else:
        interrupted = False
        for index in range(recovered.next_chunk, chunk_count(spec)):
            if token.requested:
                interrupted = True
                break
            times = chunk_times(spec, index)
            start_ms, end_ms = chunk_bounds(spec, index)
            state_at_start = state
            slice_result = simulate_frames(spec, cache, times, state)
            state = slice_result.state_at_end
            chunk = ChunkResult(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                frames=slice_result.frames,
                events=slice_result.events,
                state_at_start=state_at_start,
                state_at_end=state,
                dependencies=build_chunk_dependencies(
                    spec,
                    slice_result.frames,
                    slice_result.events,
                    start_ms,
                    end_ms,
                ),
                source="computed",
            )
            persist_chunk(directory, chunk, version)
            chunks.append(chunk)
            written.append(index)
            _write_manifest(directory, spec, version, chunks)
            if stop_after_chunks is not None and len(written) >= stop_after_chunks:
                token.request(130, reason="stop_after_chunks")
                interrupted = True
                break
        if token.requested:
            interrupted = True

    result = ReplayResult(
        spec=spec,
        version_id=version,
        chunks=chunks,
        concurrency=concurrency_report,
    )
    report: dict[str, Any] = {
        "ok": not interrupted,
        "status": "interrupted" if interrupted else "complete",
        "version_id": version,
        "resumed": recovered.next_chunk > 0,
        "recovered_chunk_count": len(recovered.chunks),
        "recovered_frame_count": recovered.recovered_frame_count,
        "recovered_event_count": recovered.recovered_event_count,
        "written_chunk_count": len(written),
        "written_chunks": written,
        "next_chunk": len(chunks),
        "chunk_count": chunk_count(spec),
        "frame_count": len(result.frames),
        "event_count": len(result.events),
        "invalid_chunks": recovered.invalid_chunks,
        "manifest_present": recovered.manifest_present,
        "exit_code": token.exit_code if interrupted else 0,
        "concurrency": concurrency_report,
    }
    if not interrupted:
        _write_final_report(directory, result, extra={"resumed": recovered.next_chunk > 0})
        report["content_digest"] = output_digest(result)
    return PersistenceResult(
        result=result,
        report=report,
        interrupted=interrupted,
        exit_code=token.exit_code if interrupted else 0,
    )


def output_digest(result: ReplayResult) -> str:
    return sha256_value(
        {
            "version_id": result.version_id,
            "frames": [frame.to_dict() for frame in result.frames],
            "events": [event.to_dict() for event in result.events],
        }
    )

