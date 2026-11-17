from __future__ import annotations

import copy
import io
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from PIL import Image

from .artifacts import Artifact, ArtifactStore, dependency, stage_cache_key
from .config import PatchSpec, ValidatedConfig, config_signature, normalized_config_for_output, validate_config
from .errors import ConfigError, InjectedStageError, WorkbenchError
from .util import (
    array_fingerprint,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_npy,
    canonical_json,
    now_iso,
    sha256_bytes,
    sha256_json,
    stable_sort_key,
)


PIPELINE_SEMANTICS_VERSION = 1
CODE_PRIORITY = [
    "E_CONFIG_INVALID",
    "E_INITIMAGE_INVALID",
    "E_PATCH_OUT_OF_RANGE",
    "E_NOISE_MISMATCH",
    "E_STAGE_FAILED",
    "E_INTERRUPTED",
    "E_IO",
    "OK",
]


@dataclass
class RenderResult:
    status: str
    business_codes: list[str]
    snapshot_version: int
    report_path: str | None
    manifest_path: str
    frames: np.ndarray
    frame_valid_mask: np.ndarray
    failures: list[dict[str, Any]]
    preview: dict[str, Any] | None
    reused_stages: list[str]
    invalidated_stages: list[str]
    recomputed_stages: list[str]
    config_fingerprint: str | None
    recovery: dict[str, Any]
    summary: dict[str, Any]

    @property
    def primary_code(self) -> str:
        return self.business_codes[0] if self.business_codes else "OK"


def prompt_embedding(config: ValidatedConfig) -> tuple[np.ndarray, dict[str, Any]]:
    """Restricted-vocabulary, hashed bag-of-words plus hashed bigrams."""

    vector = np.zeros(config.embed_dim, dtype=np.float64)
    features: list[tuple[str, float]] = [(token, 1.0) for token in config.tokens]
    features.extend(
        (f"{left}_{right}", 0.5)
        for left, right in zip(config.tokens, config.tokens[1:])
    )
    for feature, weight in features:
        digest = sha256_bytes(f"token:{feature}".encode("utf-8"))
        bucket = int(digest[:12], 16) % config.embed_dim
        sign = 1.0 if int(digest[12:14], 16) & 1 else -1.0
        vector[bucket] += sign * weight
    norm = float(np.linalg.norm(vector))
    if norm > 0.0:
        vector /= norm
    metadata = {
        "algorithm": "sha256-hashed-bag-v1",
        "tokens": list(config.tokens),
        "features": len(features),
        "embedding_dim": config.embed_dim,
        "normalized": True,
    }
    return np.ascontiguousarray(vector), metadata


def _seed_from_parts(*parts: Any) -> int:
    return int(sha256_json([PIPELINE_SEMANTICS_VERSION, *parts])[:16], 16)


def condition_latent(config: ValidatedConfig, embedding: np.ndarray) -> np.ndarray:
    total = int(np.prod(config.latent_shape))
    projection_seed = _seed_from_parts("condition-projection", config.embed_dim, config.latent_shape)
    rng = np.random.default_rng(projection_seed)
    projection = rng.standard_normal((total, config.embed_dim), dtype=np.float64)
    projection /= math.sqrt(max(1, config.embed_dim))
    base = (projection @ embedding).reshape(config.latent_shape)
    base = np.tanh(base * 1.35)
    frames = np.empty((config.frames, *config.latent_shape), dtype=np.float64)
    height = config.latent_h
    width = config.latent_w
    for frame in range(config.frames):
        shifted = np.roll(base, shift=(frame * 3 + 1) % height, axis=0)
        shifted = np.roll(shifted, shift=(frame * 5 + 2) % width, axis=1)
        phase = ((frame + 1) * (np.arange(total, dtype=np.float64) + 1.0)).reshape(config.latent_shape)
        frames[frame] = shifted + 0.035 * np.sin(phase / 11.0)
    return np.ascontiguousarray(frames)


def _image_to_latent(config: ValidatedConfig, image: np.ndarray) -> np.ndarray:
    assert config.init_image is not None
    channels: list[np.ndarray] = []
    for channel in range(3):
        source = Image.fromarray(np.ascontiguousarray(image[:, :, channel]), mode="L")
        resized = source.resize((config.latent_w, config.latent_h), resample=Image.Resampling.BILINEAR)
        channels.append(np.asarray(resized, dtype=np.float64) / 127.5 - 1.0)
    stacked = np.stack(channels, axis=-1)
    projection_rng = np.random.default_rng(_seed_from_parts("image-projection", config.latent_shape))
    projection = projection_rng.standard_normal((3, config.latent_c), dtype=np.float64) / math.sqrt(3.0)
    base = np.tanh(stacked @ projection)
    frame_offsets = np.random.default_rng(
        _seed_from_parts("image-frames", config.seed, config.frames, config.latent_shape)
    ).standard_normal((config.frames, *config.latent_shape), dtype=np.float64)
    base = np.tanh(base[None, ...] * 0.94 + frame_offsets * 0.025)
    return np.ascontiguousarray(base)


def initial_latents(
    config: ValidatedConfig,
    embedding: np.ndarray,
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray]:
    condition = condition_latent(config, embedding)
    rng = np.random.default_rng(
        _seed_from_parts("initial-latents", config.prompt, config.seed, config.frames, config.latent_shape, config.mode)
    )
    random_base = rng.standard_normal((config.frames, *config.latent_shape), dtype=np.float64)
    image_latent: np.ndarray | None = None
    if config.mode == "i2v":
        if config.init_image is None:
            raise ConfigError("i2v mode requires an initial image")
        image_latent = _image_to_latent(config, config.init_image.pixels)
        latent = np.tanh(random_base * 0.18 + image_latent * 0.76 + condition * 0.06)
    else:
        latent = np.tanh(random_base * 0.38 + condition * 0.24)
    return np.ascontiguousarray(latent), None if image_latent is None else np.ascontiguousarray(image_latent), condition


def noise_for_frame(noise: np.ndarray, step: int, frame: int) -> np.ndarray:
    height, width = noise.shape[0], noise.shape[1]
    shifted = np.roll(noise, shift=(frame * 3 + step) % height, axis=0)
    shifted = np.roll(shifted, shift=(frame * 5 + 2 * step) % width, axis=1)
    return np.ascontiguousarray(shifted, dtype=np.float64)


def denoise_step(
    previous: np.ndarray,
    noise: np.ndarray,
    condition: np.ndarray,
    *,
    guidance: float,
    step: int,
) -> np.ndarray:
    """Shared post-replacement denoise semantics for t2v and i2v."""

    return np.ascontiguousarray(
        np.tanh(
            previous * 0.965
            + noise * 0.055
            + condition * (0.0125 * guidance)
            + 0.0015 * float(step + 1)
        ),
        dtype=np.float64,
    )


def replacement_latent(image_latent: np.ndarray, condition: np.ndarray, step: int) -> np.ndarray:
    return np.ascontiguousarray(
        np.tanh(image_latent * 0.985 + condition * 0.012 + 0.0005 * float(step + 1)),
        dtype=np.float64,
    )


class Renderer:
    def __init__(self, *, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else Path.cwd().resolve()

    def render(
        self,
        raw_config: Mapping[str, Any],
        *,
        allow_reuse: bool = True,
        serial: bool = False,
        on_preview: Callable[[dict[str, Any]], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> RenderResult:
        started = time.perf_counter()
        stop_event = stop_event or threading.Event()
        try:
            config = validate_config(raw_config, self.base_dir)
        except WorkbenchError as exc:
            return self._validation_failure(raw_config, exc, started)
        if serial:
            config.max_workers = 1
        return self._render_validated(
            config,
            allow_reuse=allow_reuse,
            on_preview=on_preview,
            on_progress=on_progress,
            stop_event=stop_event,
            started=started,
        )

    def _render_validated(
        self,
        config: ValidatedConfig,
        *,
        allow_reuse: bool,
        on_preview: Callable[[dict[str, Any]], None] | None,
        on_progress: Callable[[dict[str, Any]], None] | None,
        stop_event: threading.Event,
        started: float,
    ) -> RenderResult:
        store = ArtifactStore(config.output_dir)
        stage_order = config.stage_graph()
        total_tasks = len(stage_order)
        store.begin_run(
            config_fingerprint=config.config_fingerprint,
            stage_order=stage_order,
            total_tasks=total_tasks,
        )
        failures: list[dict[str, Any]] = []
        business_codes: list[str] = []
        interrupted = False
        fatal = False
        preview_info = store.latest_preview_record(stage_order=stage_order)
        latents: dict[int, np.ndarray] = {}
        valid_masks: dict[int, np.ndarray] = {}
        decode_artifacts: dict[int, Artifact] = {}
        frames_pixels = np.zeros((config.frames, config.height, config.width, 3), dtype=np.uint8)
        frames_valid = np.zeros((config.frames,), dtype=bool)
        final_latent_artifact: Artifact | None = None
        condition_artifact: Artifact | None = None

        def emit_progress() -> None:
            if on_progress is None:
                return
            try:
                on_progress(store.progress_summary())
            except Exception:
                pass

        def simulated_stage_delay() -> None:
            if config.stage_delay_ms > 0:
                stop_event.wait(config.stage_delay_ms / 1000.0)

        emit_progress()
        try:
            # Encode stage
            if stop_event.is_set():
                interrupted = True
                raise _Interrupted()
            store.update_current_stage("encode")
            start_stage = time.perf_counter()
            simulated_stage_delay()
            deps = [dependency("vocab", config.vocab_fingerprint)]
            local = {
                "prompt": config.prompt,
                "tokens": list(config.tokens),
                "embed_dim": config.embed_dim,
                "algorithm": "sha256-hashed-bag-v1",
                "injections": _injection_cache_entries(config, "encode"),
            }
            key = stage_cache_key("encode", local, deps)
            artifact = store.lookup(
                stage="encode",
                stage_kind="encode",
                step=None,
                frame=None,
                cache_key=key,
                dependencies=deps,
            ) if allow_reuse else None
            if artifact is None:
                if _injection(config, "encode", None, None):
                    raise InjectedStageError(_injection(config, "encode", None, None)["error_type"], "encode", None, None)
                embedding, metadata = prompt_embedding(config)
                artifact = store.persist(
                    stage="encode",
                    stage_kind="encode",
                    step=None,
                    frame=None,
                    cache_key=key,
                    dependencies=deps,
                    local_inputs=local,
                    arrays={"embedding": embedding},
                    timing_ms=(time.perf_counter() - start_stage) * 1000.0,
                    extra_metadata=metadata,
                )
            condition_artifact = artifact
            emit_progress()

            # Init stage
            if stop_event.is_set():
                interrupted = True
                raise _Interrupted()
            store.update_current_stage("init")
            start_stage = time.perf_counter()
            simulated_stage_delay()
            deps = [dependency("encode", artifact)]
            if config.init_image is not None:
                deps.append(dependency("init_image", config.init_image.fingerprint))
            local = {
                "seed": config.seed,
                "mode": config.mode,
                "frames": config.frames,
                "latent_shape": list(config.latent_shape),
                "image_fingerprint": None if config.init_image is None else config.init_image.fingerprint,
                "injections": _injection_cache_entries(config, "init"),
            }
            key = stage_cache_key("init", local, deps)
            init_artifact = store.lookup(
                stage="init",
                stage_kind="init",
                step=None,
                frame=None,
                cache_key=key,
                dependencies=deps,
            ) if allow_reuse else None
            if init_artifact is None:
                if _injection(config, "init", None, None):
                    raise InjectedStageError(_injection(config, "init", None, None)["error_type"], "init", None, None)
                embedding = condition_artifact.arrays["embedding"]
                latent, image_latent, condition = initial_latents(config, embedding)
                arrays: dict[str, np.ndarray] = {"latent": latent, "condition": condition}
                if image_latent is not None:
                    arrays["image_latent"] = image_latent
                init_artifact = store.persist(
                    stage="init",
                    stage_kind="init",
                    step=None,
                    frame=None,
                    cache_key=key,
                    dependencies=deps,
                    local_inputs=local,
                    arrays=arrays,
                    timing_ms=(time.perf_counter() - start_stage) * 1000.0,
                    extra_metadata={"mode": config.mode, "seed": config.seed},
                )
            condition = init_artifact.arrays["condition"]
            image_latent = init_artifact.arrays.get("image_latent")
            emit_progress()

            # Denoise stages. Frames inside one step are evaluated concurrently.
            current_latent = init_artifact.arrays["latent"]
            current_valid = np.ones((config.frames,), dtype=bool)
            previous_artifact = init_artifact
            executor = ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="wb-denoise")
            try:
                for step in range(config.steps):
                    if stop_event.is_set():
                        interrupted = True
                        raise _Interrupted()
                    stage = f"denoise_{step:03d}"
                    store.update_current_stage(stage)
                    start_stage = time.perf_counter()
                    simulated_stage_delay()
                    replaced = config.mode == "i2v" and step < config.i2v_replacement_steps
                    patch_in_effect = config.patch is not None and step >= config.patch.step
                    patch_fp = config.patch.fingerprint if patch_in_effect else None
                    deps = [
                        dependency("encode", condition_artifact),
                        dependency(previous_artifact.stage, previous_artifact),
                        dependency(f"noise_step_{step:03d}", array_fingerprint(config.noise_table[step])),
                    ]
                    local = {
                        "step": step,
                        "guidance": config.guidance,
                        "mode": config.mode,
                        "replaced": replaced,
                        "i2v_replacement_steps": config.i2v_replacement_steps,
                        "patch_fingerprint": patch_fp,
                        "injections": _injection_cache_entries(config, "denoise", step=step),
                    }
                    key = stage_cache_key(stage, local, deps)
                    artifact = store.lookup(
                        stage=stage,
                        stage_kind="denoise",
                        step=step,
                        frame=None,
                        cache_key=key,
                        dependencies=deps,
                    ) if allow_reuse else None
                    if artifact is None:
                        latent_output = np.zeros_like(current_latent, dtype=np.float64)
                        valid_output = np.zeros((config.frames,), dtype=bool)
                        stage_failures: list[dict[str, Any]] = []
                        tasks: list[dict[str, Any]] = []
                        for frame in range(config.frames):
                            tasks.append(
                                {
                                    "stage": "denoise",
                                    "step": step,
                                    "frame": frame,
                                    "sort_key": stable_sort_key("denoise", step, frame),
                                }
                            )

                        def run_frame(task: dict[str, Any]) -> tuple[int, np.ndarray | None, str | None, str | None]:
                            frame = int(task["frame"])
                            try:
                                if not current_valid[frame]:
                                    raise _TaskDependencyError("previous denoise output is invalid")
                                injection = _injection(config, "denoise", step, frame)
                                if injection:
                                    raise InjectedStageError(injection["error_type"], "denoise", step, frame)
                                if replaced:
                                    if image_latent is None:
                                        raise RuntimeError("i2v image latent is unavailable")
                                    output = replacement_latent(image_latent[frame], condition[frame], step)
                                else:
                                    output = denoise_step(
                                        current_latent[frame],
                                        noise_for_frame(config.noise_table[step], step, frame),
                                        condition[frame],
                                        guidance=config.guidance,
                                        step=step,
                                    )
                                output = _apply_patch(config.patch, output, step, frame)
                                return frame, output, None, None
                            except InjectedStageError as exc:
                                return frame, None, exc.error_type, str(exc)
                            except _TaskDependencyError as exc:
                                return frame, None, "DependencyError", str(exc)
                            except Exception as exc:
                                return frame, None, type(exc).__name__, str(exc)

                        futures = [executor.submit(run_frame, task) for task in tasks]
                        ordered_results = []
                        for task, future in zip(tasks, futures):
                            ordered_results.append((task, future.result()))
                        ordered_results.sort(key=lambda item: tuple(item[0]["sort_key"]))
                        for task, (frame, output, error_type, message) in ordered_results:
                            if output is None:
                                stage_failures.append(
                                    _failure("denoise", step, frame, error_type or "StageError", message or "stage failed")
                                )
                            else:
                                latent_output[frame] = output
                                valid_output[frame] = True
                        stage_failures.sort(key=lambda item: tuple(item["sort_key"]))
                        failures.extend(stage_failures)
                        artifact = store.persist(
                            stage=stage,
                            stage_kind="denoise",
                            step=step,
                            frame=None,
                            cache_key=key,
                            dependencies=deps,
                            local_inputs=local,
                            arrays={"latent": latent_output, "valid_mask": valid_output},
                            timing_ms=(time.perf_counter() - start_stage) * 1000.0,
                            extra_metadata={"failures": stage_failures, "replaced": replaced},
                        )
                    else:
                        failures.extend(copy.deepcopy(artifact.metadata.get("extra", {}).get("failures", [])))
                    current_latent = artifact.arrays["latent"]
                    current_valid = artifact.arrays["valid_mask"].astype(bool, copy=False)
                    latents[step] = current_latent
                    valid_masks[step] = current_valid
                    previous_artifact = artifact
                    emit_progress()

                final_latent_artifact = previous_artifact
                if stop_event.is_set():
                    interrupted = True
                    raise _Interrupted()

                # Decode frame zero first so a fresh low-resolution preview can be emitted early.
                frame_zero = self._decode_frame(
                    config=config,
                    frame=0,
                    final_artifact=final_latent_artifact,
                    condition_artifact=condition_artifact,
                    store=store,
                    executor=executor,
                    allow_reuse=allow_reuse,
                    failures=failures,
                )
                decode_artifacts[0] = frame_zero
                frames_pixels[0] = frame_zero.arrays["pixels"]
                frames_valid[0] = bool(frame_zero.arrays["valid_mask"][0])
                if stop_event.is_set():
                    interrupted = True
                    raise _Interrupted()
                latest_preview = self._preview_stage(
                    config=config,
                    frame_zero=frame_zero,
                    condition_artifact=condition_artifact,
                    store=store,
                    allow_reuse=allow_reuse,
                    failures=failures,
                )
                if latest_preview is not None:
                    preview_info = latest_preview
                    if on_preview is not None:
                        try:
                            on_preview(latest_preview)
                        except Exception:
                            pass
                emit_progress()

                if stop_event.is_set():
                    interrupted = True
                    raise _Interrupted()

                remaining_frames = list(range(1, config.frames))
                remaining_tasks = [
                    {"frame": frame, "sort_key": stable_sort_key("decode", None, frame)}
                    for frame in remaining_frames
                ]
                if remaining_tasks:
                    future_map = [
                        (
                            task,
                            executor.submit(
                                self._decode_frame,
                                config=config,
                                frame=int(task["frame"]),
                                final_artifact=final_latent_artifact,
                                condition_artifact=condition_artifact,
                                store=store,
                                executor=executor,
                                allow_reuse=allow_reuse,
                                failures=None,
                            ),
                        )
                        for task in remaining_tasks
                    ]
                    ordered_decode = []
                    for task, future in future_map:
                        ordered_decode.append((task, future.result()))
                    ordered_decode.sort(key=lambda item: tuple(item[0]["sort_key"]))
                    for task, artifact in ordered_decode:
                        frame = int(task["frame"])
                        decode_artifacts[frame] = artifact
                        frames_pixels[frame] = artifact.arrays["pixels"]
                        frames_valid[frame] = bool(artifact.arrays["valid_mask"][0])
                        failures.extend(copy.deepcopy(artifact.metadata.get("extra", {}).get("failures", [])))
                emit_progress()
            finally:
                executor.shutdown(wait=True, cancel_futures=False)

            if stop_event.is_set():
                interrupted = True

        except _Interrupted:
            interrupted = True
        except WorkbenchError as exc:
            failure_stage, failure_step, failure_frame = _failure_location(
                store.progress_summary().get("current_stage")
            )
            failures.append(
                _failure(failure_stage, failure_step, failure_frame, type(exc).__name__, exc.message, code=exc.code)
            )
            business_codes.append(exc.code)
            fatal = True
        except InjectedStageError as exc:
            failures.append(
                _failure(exc.stage, exc.step, exc.frame, exc.error_type, str(exc), code="E_STAGE_FAILED")
            )
            business_codes.append("E_STAGE_FAILED")
            fatal = True
        except Exception as exc:
            failure_stage, failure_step, failure_frame = _failure_location(
                store.progress_summary().get("current_stage")
            )
            failures.append(
                _failure(failure_stage, failure_step, failure_frame, type(exc).__name__, str(exc), code="E_STAGE_FAILED")
            )
            business_codes.append("E_STAGE_FAILED")
            fatal = True

        # Rehydrate frames from completed decode artifacts when possible (also after interrupted runs).
        for frame, artifact in sorted(decode_artifacts.items()):
            frames_pixels[frame] = artifact.arrays["pixels"]
            frames_valid[frame] = bool(artifact.arrays["valid_mask"][0])

        failures = sorted(failures, key=lambda item: tuple(item["sort_key"]))
        stage_failure = any(item.get("business_code") == "E_STAGE_FAILED" for item in failures)
        if stage_failure:
            business_codes.append("E_STAGE_FAILED")
        if interrupted:
            business_codes.append("E_INTERRUPTED")
        business_codes = _dedupe_codes(business_codes)
        if not business_codes:
            business_codes = ["OK"]

        if interrupted:
            status = "interrupted"
        elif fatal:
            status = "failed"
        elif stage_failure or not bool(np.all(frames_valid)):
            status = "partial"
        else:
            status = "completed"

        preview_info = self._best_preview(store, preview_info)
        output_info = self._write_outputs(
            config,
            store,
            frames_pixels,
            frames_valid,
            preview_info,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        report = self._build_report(
            config=config,
            store=store,
            status=status,
            business_codes=business_codes,
            failures=failures,
            frames=frames_pixels,
            frames_valid=frames_valid,
            preview=preview_info,
            output_info=output_info,
            elapsed_ms=elapsed_ms,
            interrupted=interrupted,
        )
        atomic_write_json(store.report_path, report)
        store.finalize(
            status=status,
            business_codes=business_codes,
            report_path=str(store.report_path),
            extra={"frame_fingerprint": array_fingerprint(frames_pixels), "valid_frame_count": int(frames_valid.sum())},
        )
        summary = report["summary"]
        return RenderResult(
            status=status,
            business_codes=business_codes,
            snapshot_version=store.snapshot_version,
            report_path=str(store.report_path),
            manifest_path=str(store.manifest_path),
            frames=frames_pixels,
            frame_valid_mask=frames_valid,
            failures=failures,
            preview=preview_info,
            reused_stages=store.reused_stages,
            invalidated_stages=store.invalidated_stages,
            recomputed_stages=store.recomputed_stages,
            config_fingerprint=config.config_fingerprint,
            recovery=store.recovery,
            summary=summary,
        )

    # ------------------------------------------------------------------ stage helpers
    def _decode_frame(
        self,
        *,
        config: ValidatedConfig,
        frame: int,
        final_artifact: Artifact,
        condition_artifact: Artifact,
        store: ArtifactStore,
        executor: ThreadPoolExecutor,
        allow_reuse: bool,
        failures: list[dict[str, Any]] | None,
    ) -> Artifact:
        stage = f"decode_frame_{frame:03d}"
        store.update_current_stage(stage)
        started = time.perf_counter()
        if config.stage_delay_ms > 0:
            time.sleep(config.stage_delay_ms / 1000.0)
        deps = [
            dependency("encode", condition_artifact),
            dependency(final_artifact.stage, final_artifact),
        ]
        projection_config = _decode_projection_config(config)
        local = {
            "frame": frame,
            "output_size": list(config.output_size),
            "projection_fingerprint": projection_config["fingerprint"],
            "patch_fingerprint": None if config.patch is None else config.patch.fingerprint,
            "injections": _injection_cache_entries(config, "decode", frame=frame),
        }
        key = stage_cache_key(stage, local, deps)
        artifact = store.lookup(
            stage=stage,
            stage_kind="decode",
            step=None,
            frame=frame,
            cache_key=key,
            dependencies=deps,
        ) if allow_reuse else None
        if artifact is not None:
            if failures is not None:
                failures.extend(copy.deepcopy(artifact.metadata.get("extra", {}).get("failures", [])))
            return artifact

        stage_failure: dict[str, Any] | None = None
        pixels = np.zeros((config.height, config.width, 3), dtype=np.uint8)
        valid = False
        try:
            injection = _injection(config, "decode", None, frame)
            if injection:
                raise InjectedStageError(injection["error_type"], "decode", None, frame)
            latent_valid = bool(final_artifact.arrays["valid_mask"][frame])
            if not latent_valid:
                raise _TaskDependencyError("final latent for this frame is invalid")
            pixels_value = _decode_pixels(config, final_artifact.arrays["latent"][frame], projection_config)
            pixels = pixels_value
            valid = True
        except InjectedStageError as exc:
            stage_failure = _failure("decode", None, frame, exc.error_type, str(exc))
        except _TaskDependencyError as exc:
            stage_failure = _failure("decode", None, frame, "DependencyError", str(exc))
        except Exception as exc:
            stage_failure = _failure("decode", None, frame, type(exc).__name__, str(exc))
        if stage_failure is not None:
            if failures is not None:
                failures.append(stage_failure)
        artifact = store.persist(
            stage=stage,
            stage_kind="decode",
            step=None,
            frame=frame,
            cache_key=key,
            dependencies=deps,
            local_inputs=local,
            arrays={"pixels": pixels, "valid_mask": np.asarray([valid], dtype=bool)},
            timing_ms=(time.perf_counter() - started) * 1000.0,
            extra_metadata={"failures": [] if stage_failure is None else [stage_failure]},
        )
        return artifact

    def _preview_stage(
        self,
        *,
        config: ValidatedConfig,
        frame_zero: Artifact,
        condition_artifact: Artifact,
        store: ArtifactStore,
        allow_reuse: bool,
        failures: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        stage = "preview"
        store.update_current_stage(stage)
        started = time.perf_counter()
        if config.stage_delay_ms > 0:
            time.sleep(config.stage_delay_ms / 1000.0)
        deps = [
            dependency("encode", condition_artifact),
            dependency("decode_frame_000", frame_zero),
        ]
        local = {
            "preview_size": list(config.preview_size),
            "method": "nearest-neighbour-v1",
            "patch_fingerprint": None if config.patch is None else config.patch.fingerprint,
            "injections": _injection_cache_entries(config, "preview"),
        }
        key = stage_cache_key(stage, local, deps)
        artifact = store.lookup(
            stage=stage,
            stage_kind="preview",
            step=None,
            frame=0,
            cache_key=key,
            dependencies=deps,
        ) if allow_reuse else None
        if artifact is None:
            stage_failure: dict[str, Any] | None = None
            pixels = np.zeros((config.preview_size[1], config.preview_size[0], 3), dtype=np.uint8)
            valid = False
            try:
                injection = _injection(config, "preview", None, 0)
                if injection:
                    raise InjectedStageError(injection["error_type"], "preview", None, 0)
                if not bool(frame_zero.arrays["valid_mask"][0]):
                    raise _TaskDependencyError("decoded first frame is invalid")
                source = Image.fromarray(np.ascontiguousarray(frame_zero.arrays["pixels"]), mode="RGB")
                resized = source.resize(
                    (config.preview_size[0], config.preview_size[1]),
                    resample=Image.Resampling.NEAREST,
                )
                pixels = np.asarray(resized, dtype=np.uint8).copy()
                valid = True
            except InjectedStageError as exc:
                stage_failure = _failure("preview", None, 0, exc.error_type, str(exc))
            except _TaskDependencyError as exc:
                stage_failure = _failure("preview", None, 0, "DependencyError", str(exc))
            except Exception as exc:
                stage_failure = _failure("preview", None, 0, type(exc).__name__, str(exc))
            if stage_failure is not None:
                failures.append(stage_failure)
            artifact = store.persist(
                stage=stage,
                stage_kind="preview",
                step=None,
                frame=0,
                cache_key=key,
                dependencies=deps,
                local_inputs=local,
                arrays={"pixels": pixels, "valid_mask": np.asarray([valid], dtype=bool)},
                timing_ms=(time.perf_counter() - started) * 1000.0,
                extra_metadata={"failures": [] if stage_failure is None else [stage_failure]},
            )
        else:
            failures.extend(copy.deepcopy(artifact.metadata.get("extra", {}).get("failures", [])))
        if not bool(artifact.arrays["valid_mask"][0]):
            return None
        return self._emit_preview(config, store, artifact)

    def _emit_preview(self, config: ValidatedConfig, store: ArtifactStore, artifact: Artifact) -> dict[str, Any]:
        preview_dir = config.output_dir / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        image_path = preview_dir / f"preview_v{artifact.version:06d}.png"
        metadata_path = preview_dir / f"preview_v{artifact.version:06d}.json"
        image = Image.fromarray(np.ascontiguousarray(artifact.arrays["pixels"]), mode="RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False)
        atomic_write_bytes(image_path, buffer.getvalue())
        info = {
            "snapshot_version": artifact.version,
            "config_fingerprint": config.config_fingerprint,
            "content_fingerprint": artifact.content_fingerprint,
            "cache_key": artifact.cache_key,
            "width": config.preview_size[0],
            "height": config.preview_size[1],
            "path": str(image_path),
            "metadata_path": str(metadata_path),
            "frame": 0,
            "created_at": now_iso(),
        }
        atomic_write_json(metadata_path, info)
        atomic_write_json(preview_dir / "preview_latest.json", info)
        atomic_write_bytes(preview_dir / "preview.png", buffer.getvalue())
        atomic_write_json(preview_dir / "preview.json", info)
        return info

    # ------------------------------------------------------------------ outputs/report
    def _write_outputs(
        self,
        config: ValidatedConfig,
        store: ArtifactStore,
        frames: np.ndarray,
        valid: np.ndarray,
        preview: dict[str, Any] | None,
    ) -> dict[str, Any]:
        output_dir = config.output_dir
        previous_frames: np.ndarray | None = None
        previous_valid: np.ndarray | None = None
        try:
            previous_frames = np.load(output_dir / "frames.npy", allow_pickle=False)
            previous_valid = np.load(output_dir / "frames_valid_mask.npy", allow_pickle=False).astype(bool, copy=False)
        except (OSError, ValueError):
            pass
        atomic_write_npy(output_dir / "frames.npy", frames)
        atomic_write_npy(output_dir / f"frames_v{store.snapshot_version:06d}.npy", frames)
        atomic_write_npy(output_dir / "frames_valid_mask.npy", valid)
        frame_dir = output_dir / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_png_paths: list[str] = []
        for frame in range(config.frames):
            image = Image.fromarray(np.ascontiguousarray(frames[frame]), mode="RGB")
            image_buffer = io.BytesIO()
            image.save(image_buffer, format="PNG", optimize=False)
            frame_path = frame_dir / f"frame_{frame:03d}.png"
            atomic_write_bytes(frame_path, image_buffer.getvalue())
            frame_png_paths.append(str(frame_path))
        for existing in frame_dir.glob("frame_*.png"):
            try:
                index = int(existing.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if index >= config.frames:
                try:
                    existing.unlink()
                except OSError:
                    pass
        atomic_write_json(
            output_dir / "frames_meta.json",
            {
                "snapshot_version": store.snapshot_version,
                "config_fingerprint": config.config_fingerprint,
                "frame_count": config.frames,
                "valid_frame_count": int(valid.sum()),
                "frame_fingerprint": array_fingerprint(frames),
                "valid_mask_fingerprint": array_fingerprint(valid),
                "per_frame_valid": valid.tolist(),
                "frame_png_paths": frame_png_paths,
            },
        )
        gif_path: str | None = None
        contact_path: str | None = None
        if bool(np.any(valid)):
            images = [Image.fromarray(np.ascontiguousarray(frames[index]), mode="RGB") for index in range(config.frames) if valid[index]]
            if images:
                gif_buffer = io.BytesIO()
                images[0].save(
                    gif_buffer,
                    format="GIF",
                    save_all=True,
                    append_images=images[1:],
                    duration=180,
                    loop=0,
                    optimize=False,
                )
                gif_bytes = gif_buffer.getvalue()
                gif_path = str(output_dir / "preview_sequence.gif")
                atomic_write_bytes(gif_path, gif_bytes)
                versioned_gif = output_dir / f"preview_sequence_v{store.snapshot_version:06d}.gif"
                atomic_write_bytes(versioned_gif, gif_bytes)
                contact_sheet = _contact_sheet(images)
                contact_buffer = io.BytesIO()
                contact_sheet.save(contact_buffer, format="PNG", optimize=False)
                contact_bytes = contact_buffer.getvalue()
                contact_path = str(output_dir / "preview_sequence.png")
                atomic_write_bytes(contact_path, contact_bytes)
                atomic_write_bytes(output_dir / f"preview_sequence_v{store.snapshot_version:06d}.png", contact_bytes)
        diff = _frame_diff(previous_frames, previous_valid, frames, valid)
        return {
            "frames_path": str(output_dir / "frames.npy"),
            "valid_mask_path": str(output_dir / "frames_valid_mask.npy"),
            "frame_png_paths": frame_png_paths,
            "frame_directory": str(frame_dir),
            "gif_path": gif_path,
            "contact_sheet_path": contact_path,
            "preview_path": None if preview is None else preview.get("path"),
            "frame_diff": diff,
        }

    def _build_report(
        self,
        *,
        config: ValidatedConfig,
        store: ArtifactStore,
        status: str,
        business_codes: list[str],
        failures: list[dict[str, Any]],
        frames: np.ndarray,
        frames_valid: np.ndarray,
        preview: dict[str, Any] | None,
        output_info: Mapping[str, Any],
        elapsed_ms: float,
        interrupted: bool,
    ) -> dict[str, Any]:
        artifact_records = store.artifact_records()
        latent_records = [record for record in artifact_records if record.get("stage_kind") == "denoise"]
        stable_summary = {
            "status": status,
            "business_codes": business_codes,
            "reused_stages": store.reused_stages,
            "invalidated_stages": store.invalidated_stages,
            "recomputed_stages": store.recomputed_stages,
            "failure_manifest": failures,
            "artifact_fingerprints": [
                [record["stage"], record["content_fingerprint"]] for record in artifact_records
            ],
            "latent_fingerprints": [[record["stage"], record["content_fingerprint"]] for record in latent_records],
            "frame_fingerprint": array_fingerprint(frames),
            "valid_mask_fingerprint": array_fingerprint(frames_valid),
            "valid_frame_count": int(frames_valid.sum()),
            "preview_fingerprint": None if preview is None else preview.get("content_fingerprint"),
            "frame_diff_summary": _stable_frame_diff(output_info.get("frame_diff", {})),
        }
        stable_summary["diagnostic_signature"] = sha256_json(stable_summary)
        return {
            "schema_version": 1,
            "workbench": "local-deterministic-video-workbench",
            "generated_at": now_iso(),
            "elapsed_ms": round(elapsed_ms, 6),
            "status": status,
            "interrupted": interrupted,
            "business_codes": business_codes,
            "primary_business_code": business_codes[0],
            "snapshot_version": store.snapshot_version,
            "config": normalized_config_for_output(config),
            "config_fingerprint": config.config_fingerprint,
            "recovery_point": store.recovery,
            "reused_stages": store.reused_stages,
            "invalidated_stages": store.invalidated_stages,
            "invalidation_reasons": store.invalidation_reasons(),
            "recomputed_stages": store.recomputed_stages,
            "stage_timings_ms": store.stage_timings(),
            "stage_events": store.events(),
            "artifacts": artifact_records,
            "failure_manifest": failures,
            "progress": store.progress_summary(),
            "output": dict(output_info),
            "summary": stable_summary,
        }

    def _validation_failure(
        self,
        raw_config: Mapping[str, Any],
        error: WorkbenchError,
        started: float,
    ) -> RenderResult:
        output_dir: Path | None = None
        raw_output = raw_config.get("output_dir") if isinstance(raw_config, Mapping) else None
        if isinstance(raw_output, str):
            candidate = Path(raw_output).expanduser()
            output_dir = candidate.resolve() if candidate.is_absolute() else (self.base_dir / candidate).resolve()
        failure = _failure(error.code, None, None, type(error).__name__, error.message, code=error.code)
        report: dict[str, Any] = {
            "schema_version": 1,
            "workbench": "local-deterministic-video-workbench",
            "generated_at": now_iso(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 6),
            "status": "failed",
            "interrupted": False,
            "business_codes": [error.code],
            "primary_business_code": error.code,
            "snapshot_version": None,
            "config_fingerprint": config_signature(raw_config) if isinstance(raw_config, Mapping) else None,
            "recovery_point": None,
            "reused_stages": [],
            "invalidated_stages": [],
            "invalidation_reasons": {},
            "recomputed_stages": [],
            "stage_timings_ms": {},
            "stage_events": [],
            "artifacts": [],
            "failure_manifest": [failure],
            "progress": {},
            "output": {},
            "error": error.as_dict(),
            "summary": {
                "status": "failed",
                "business_codes": [error.code],
                "reused_stages": [],
                "invalidated_stages": [],
                "recomputed_stages": [],
                "failure_manifest": [failure],
                "artifact_fingerprints": [],
                "latent_fingerprints": [],
                "frame_fingerprint": None,
                "valid_mask_fingerprint": None,
                "valid_frame_count": 0,
                "preview_fingerprint": None,
                "frame_diff_summary": {},
            },
        }
        report["summary"]["diagnostic_signature"] = sha256_json(report["summary"])
        report_path: str | None = None
        manifest_path = ""
        if output_dir is not None:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                report_path = str(output_dir / "diagnostic_report.json")
                manifest_path = str(output_dir / "manifest.json")
                atomic_write_json(report_path, report)
                if not Path(manifest_path).exists():
                    atomic_write_json(
                        Path(manifest_path),
                        {
                            "schema_version": 1,
                            "snapshot_version": 0,
                            "status": "failed",
                            "business_codes": [error.code],
                            "pid": None,
                            "updated_at": now_iso(),
                            "artifacts": {},
                            "progress": {},
                            "final": {"validation_failed": True},
                        },
                    )
            except OSError:
                report_path = None
        return RenderResult(
            status="failed",
            business_codes=[error.code],
            snapshot_version=0,
            report_path=report_path,
            manifest_path=manifest_path,
            frames=np.zeros((0, 0, 0, 3), dtype=np.uint8),
            frame_valid_mask=np.zeros((0,), dtype=bool),
            failures=[failure],
            preview=None,
            reused_stages=[],
            invalidated_stages=[],
            recomputed_stages=[],
            config_fingerprint=None,
            recovery={},
            summary=report["summary"],
        )

    def _best_preview(self, store: ArtifactStore, preview: dict[str, Any] | None) -> dict[str, Any] | None:
        if preview is not None:
            return preview
        return store.latest_preview_record()


class _Interrupted(Exception):
    pass


class _TaskDependencyError(RuntimeError):
    pass


def _injection(config: ValidatedConfig, stage: str, step: int | None, frame: int | None) -> dict[str, Any] | None:
    return config.injection_for(stage, step, frame)


def _injection_cache_entries(
    config: ValidatedConfig,
    stage: str,
    *,
    step: int | None = None,
    frame: int | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in config.inject_failures:
        if item.get("stage") != stage:
            continue
        if step is not None and item.get("step") is not None and item["step"] != step:
            continue
        if frame is not None and item.get("frame") is not None and item["frame"] != frame:
            continue
        entries.append(dict(item))
    return sorted(entries, key=lambda item: canonical_json(item))


def _apply_patch(patch: PatchSpec | None, latent: np.ndarray, step: int, frame: int) -> np.ndarray:
    if patch is None or not patch.applies_to(step, frame):
        return latent
    output = latent.copy()
    output[patch.y : patch.y + patch.height, patch.x : patch.x + patch.width, :] = patch.values
    return np.ascontiguousarray(output)


def _decode_projection_config(config: ValidatedConfig) -> dict[str, Any]:
    input_dim = int(np.prod(config.latent_shape))
    output_dim = config.height * config.width * 3
    seed = _seed_from_parts("decode-projection", config.latent_shape, config.output_size)
    fingerprint = sha256_json(
        {
            "algorithm": "linear-projection-v1",
            "seed": seed,
            "input_dim": input_dim,
            "output_dim": output_dim,
            "output_size": list(config.output_size),
        }
    )
    return {
        "seed": seed,
        "input_dim": input_dim,
        "output_dim": output_dim,
        "fingerprint": fingerprint,
        "algorithm": "linear-projection-v1",
    }


def _decode_pixels(config: ValidatedConfig, latent: np.ndarray, projection_config: Mapping[str, Any]) -> np.ndarray:
    rng = np.random.default_rng(int(projection_config["seed"]))
    input_dim = int(projection_config["input_dim"])
    output_dim = int(projection_config["output_dim"])
    projection = rng.standard_normal((input_dim, output_dim), dtype=np.float64) / math.sqrt(input_dim)
    bias = rng.standard_normal((output_dim,), dtype=np.float64) * 3.0
    values = np.asarray(latent, dtype=np.float64).reshape(input_dim) @ projection + bias
    values = values.reshape(config.height, config.width, 3)
    values = np.clip(values * 36.0 + 127.5, 0.0, 255.0)
    return np.ascontiguousarray(np.rint(values).astype(np.uint8))


def _failure(
    stage: str,
    step: int | None,
    frame: int | None,
    error_type: str,
    message: str,
    *,
    code: str = "E_STAGE_FAILED",
) -> dict[str, Any]:
    return {
        "sort_key": stable_sort_key(stage, step, frame),
        "stage": stage,
        "step": step,
        "frame": frame,
        "error_type": error_type,
        "message": message,
        "business_code": code,
    }


def _failure_location(current_stage: Any) -> tuple[str, int | None, int | None]:
    if not isinstance(current_stage, str) or not current_stage:
        return "pipeline", None, None
    if current_stage.startswith("denoise_"):
        try:
            return "denoise", int(current_stage.split("_", 1)[1]), None
        except (IndexError, ValueError):
            return current_stage, None, None
    if current_stage.startswith("decode_frame_"):
        try:
            return "decode", None, int(current_stage.split("_", 2)[2])
        except (IndexError, ValueError):
            return current_stage, None, None
    if current_stage == "preview":
        return "preview", None, 0
    return current_stage, None, None


def _dedupe_codes(codes: list[str]) -> list[str]:
    unique = set(codes)
    ordered = [code for code in CODE_PRIORITY if code in unique]
    ordered.extend(sorted(unique - set(ordered)))
    return ordered


def _contact_sheet(images: list[Image.Image]) -> Image.Image:
    width = max(image.width for image in images)
    height = max(image.height for image in images)
    columns = min(4, len(images))
    rows = int(math.ceil(len(images) / columns))
    sheet = Image.new("RGB", (columns * width, rows * height), (0, 0, 0))
    for index, image in enumerate(images):
        left = (index % columns) * width
        top = (index // columns) * height
        sheet.paste(image, (left, top))
    return sheet


def _frame_diff(
    previous: np.ndarray | None,
    previous_valid: np.ndarray | None,
    current: np.ndarray,
    current_valid: np.ndarray,
) -> dict[str, Any]:
    if previous is None or previous_valid is None:
        return {
            "has_previous": False,
            "same": None,
            "previous_frame_count": 0,
            "current_frame_count": int(current.shape[0]),
            "changed_frames": None,
            "changed_pixels": None,
            "max_abs_diff": None,
            "mean_abs_diff": None,
            "per_frame": [],
        }
    if previous.shape[1:] != current.shape[1:]:
        return {
            "has_previous": True,
            "same": False,
            "previous_frame_count": int(previous.shape[0]),
            "current_frame_count": int(current.shape[0]),
            "changed_frames": max(int(previous.shape[0]), int(current.shape[0])),
            "changed_pixels": None,
            "max_abs_diff": None,
            "mean_abs_diff": None,
            "per_frame": [
                {"frame": frame, "status": "frame_shape_changed", "changed_pixels": None}
                for frame in range(max(int(previous.shape[0]), int(current.shape[0])))
            ],
            "previous_frame_shape": list(previous.shape[1:]),
            "current_frame_shape": list(current.shape[1:]),
        }
    previous_count = int(previous.shape[0])
    current_count = int(current.shape[0])
    common = min(previous_count, current_count)
    per_frame: list[dict[str, Any]] = []
    total_changed_pixels = 0
    maximum = 0
    absolute_sum = 0.0
    compared_values = 0
    changed_frames = 0
    all_equal = previous_count == current_count and bool(np.array_equal(previous_valid, current_valid))
    for frame in range(max(previous_count, current_count)):
        if frame >= previous_count or frame >= current_count:
            changed_frames += 1
            per_frame.append({"frame": frame, "status": "missing_on_one_side", "changed_pixels": None})
            all_equal = False
            continue
        left = previous[frame].astype(np.int16)
        right = current[frame].astype(np.int16)
        difference = np.abs(left - right)
        changed = int(np.count_nonzero(np.any(difference != 0, axis=-1)))
        frame_max = int(difference.max()) if difference.size else 0
        frame_sum = float(difference.sum())
        total_changed_pixels += changed
        maximum = max(maximum, frame_max)
        absolute_sum += frame_sum
        compared_values += int(difference.size)
        if changed or not bool(previous_valid[frame]) == bool(current_valid[frame]):
            changed_frames += 1
            all_equal = False
        per_frame.append(
            {
                "frame": frame,
                "status": "changed" if changed else "same",
                "changed_pixels": changed,
                "max_abs_diff": frame_max,
            }
        )
    return {
        "has_previous": True,
        "same": all_equal,
        "previous_frame_count": previous_count,
        "current_frame_count": current_count,
        "changed_frames": changed_frames,
        "changed_pixels": total_changed_pixels,
        "max_abs_diff": maximum,
        "mean_abs_diff": round(absolute_sum / compared_values, 9) if compared_values else 0.0,
        "per_frame": per_frame,
        "compared_frames": common,
    }


def _stable_frame_diff(diff: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "has_previous": diff.get("has_previous"),
        "same": diff.get("same"),
        "previous_frame_count": diff.get("previous_frame_count"),
        "current_frame_count": diff.get("current_frame_count"),
        "changed_frames": diff.get("changed_frames"),
        "changed_pixels": diff.get("changed_pixels"),
        "max_abs_diff": diff.get("max_abs_diff"),
        "mean_abs_diff": diff.get("mean_abs_diff"),
        "per_frame": diff.get("per_frame", []),
    }


def force_serial_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(dict(raw_config))
    updated["max_workers"] = 1
    return updated
