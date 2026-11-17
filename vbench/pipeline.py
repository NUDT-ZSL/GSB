"""双路径多档位确定性渲染主管线。

阶段划分：init（初始化）-> denoise（逐去噪步）-> upgrade（档位升级）
-> decode（解码）。每阶段产物经临时文件原子替换，线程池仅负责调度，
所有计算由稳定哈希派生，因此并发度/调度顺序不改变最终结果。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import denoise as dn
from .config import Config, Tier
from .embedding import HashBagOfWords
from .errors import ErrorCode, Failure
from .hashing import fingerprint_array, stable_hash
from .latents import (InitArtifact, build_encoder, initialize_image_latent,
                      initialize_text_latent)
from .samples import Clip
from .scheduler import (CROSS_CLIP, FRAME_BATCH, SERIAL, TierPlan,
                        preview_plan, schedule)
from .snapshot import SnapshotStore

TEXT = dn.TEXT
IMAGE = dn.IMAGE


@dataclass
class LatentPatch:
    """局部潜变量补丁（需求 6 编辑序列的一种）。"""
    clip_id: str
    tier: str
    path: str
    step: int
    frame: int
    channel: int
    delta: float

    def fingerprint(self) -> str:
        return stable_hash("patch", self.clip_id, self.tier, self.path,
                           self.step, self.frame, self.channel, self.delta)


@dataclass
class RenderRequest:
    sample_dir: str
    output_dir: str
    clips: List[Clip]
    load_failures: List[Failure]
    prompt_overrides: Dict[str, str] = field(default_factory=dict)
    latent_patches: List[LatentPatch] = field(default_factory=list)
    stop_event: Optional[threading.Event] = None


@dataclass
class ClipResult:
    clip_id: str
    embeds_fingerprint: str
    init_artifacts: Dict[str, Dict[str, InitArtifact]]
    tier_reuse: List[Dict[str, Any]]
    segments: Dict[str, Any]
    divergence: List[Failure]
    decoded: Dict[str, Dict[str, np.ndarray]]
    steps_done: Dict[Tuple[str, str], int]
    order_key: Tuple[Any, ...]
    task_keys: List[Tuple[int, int, int, int, int]] = field(default_factory=list)


def _stable_key(clip_index: int, tier_index: int, path_index: int,
                step: int, frame: int) -> Tuple[int, int, int, int, int]:
    return (clip_index, tier_index, path_index, step, frame)


def _frame_groups(mode: str, frames: int, clip_count: int
                  ) -> List[List[int]]:
    if mode == SERIAL:
        return [[index] for index in range(frames)]
    if mode == FRAME_BATCH:
        return [list(range(frames))]
    if mode == CROSS_CLIP:
        # 跨片段合并在任务提交层处理；帧维仍整批。
        return [list(range(frames))]
    raise ValueError(f"未知批量模式 {mode}")


class Pipeline:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.encoder: HashBagOfWords = build_encoder(config)
        self.decision = schedule(config, 0)  # 占位，运行时按片段数重算

    # ---------------- 阶段 1：初始化 ----------------
    def _stage_init(self, clip: Clip, tier: Tier, prompt: str
                    ) -> Dict[str, InitArtifact]:
        embed = self.encoder.embed(prompt)
        text_artifact = initialize_text_latent(
            self.config, clip.clip_id, embed, tier)
        image_artifact = initialize_image_latent(
            self.config, clip.clip_id,
            clip.init_images[tier.name],
            clip.init_image_fingerprints[tier.name], tier)
        return {TEXT: text_artifact, IMAGE: image_artifact,
                "_embed_fp": fingerprint_array(embed)}

    def _persist_init(self, store: SnapshotStore, clip_id: str, tier: str,
                      artifacts: Dict[str, InitArtifact]) -> None:
        for path in (TEXT, IMAGE):
            artifact = artifacts[path]
            rel = os.path.join("stages", "init", clip_id, tier, f"{path}.raw")
            store.save_array(rel, artifact.latent)
            store.atomic_json(
                os.path.join("stages", "init", clip_id, tier,
                             f"{path}.manifest.json"),
                artifact.to_manifest())

    # ---------------- 阶段 2/3：逐档去噪与升级 ----------------
    def _run_steps(self, initial: np.ndarray, steps: Sequence[int],
                   kind: str, tier: Tier, clip: Clip, motion: np.ndarray,
                   mode: str, clip_count: int,
                   states: Optional[Dict[int, np.ndarray]] = None
                   ) -> Dict[int, np.ndarray]:
        # 无论批量调度模式如何，去噪函数始终作用于完整帧数组，保证
        # 逐帧串行/帧维批量/跨片段合并三条路径的浮点运算逐元素一致；
        # 模式差异仅体现在线程池任务划分（见 run() 的提交方式）。
        result: Dict[int, np.ndarray] = states if states is not None else {}
        all_frames = list(range(self.config.frames))
        current = initial
        for step in steps:
            if kind == "shared":
                current = dn.shared_step(
                    current, all_frames, step, self.config, tier,
                    clip.clip_id, motion)
            else:
                current = dn.replace_step(
                    current, all_frames, step, self.config, tier,
                    clip.clip_id, motion)
            result[step] = current
        return result

    def _tier_zero(self, clip: Clip, tier: Tier,
                   init: Dict[str, InitArtifact], mode: str,
                   clip_count: int
                   ) -> Tuple[Dict[int, np.ndarray], Dict[int, np.ndarray],
                              List[Failure]]:
        motion = dn.motion_matrix(clip)
        replace = self.config.img2img_replace_steps
        total = self.config.denoise_steps
        divergent_steps = list(range(replace))
        shared_steps = list(range(replace, total))

        text_states = self._run_steps(
            init[TEXT].latent, range(total), "shared", tier, clip,
            motion, mode, clip_count)
        image_states: Dict[int, np.ndarray] = {}
        if replace > 0:
            image_states = self._run_steps(
                init[IMAGE].latent, divergent_steps, "replace", tier, clip,
                motion, mode, clip_count, image_states)
            # 替代步结束后锚定到文生同一步潜变量，后续共享同一计算。
            anchor = text_states[replace - 1].copy()
            image_states[replace - 1] = anchor
        else:
            # 无替代步：两条路径从同一文生起点进入完全共享段。
            anchor = init[TEXT].latent.copy()
        image_states = self._run_steps(
            anchor, shared_steps, "shared", tier, clip, motion,
            mode, clip_count, image_states)

        failures = self._verify_segments(
            clip, tier, text_states, image_states)
        return text_states, image_states, failures

    def _verify_segments(self, clip: Clip, tier: Tier,
                         text_states: Dict[int, np.ndarray],
                         image_states: Dict[int, np.ndarray]
                         ) -> List[Failure]:
        replace = self.config.img2img_replace_steps
        total = self.config.denoise_steps
        failures: List[Failure] = []
        corrupt = self.config.fault_inject.get("corrupt_shared_step")
        for step in range(total):
            if step not in text_states or step not in image_states:
                continue
            image_state = image_states[step]
            if corrupt is not None and step == int(corrupt):
                image_state = image_state + np.float32(1.0)
            diff = dn.max_channel_diff_latent(text_states[step], image_state)
            if step >= replace and diff != 0.0:
                failures.append(Failure(
                    code=ErrorCode.E_PATH_DIVERGENCE,
                    message=(f"共享段步 {step} 文生/图生潜变量不一致，"
                             f"最大差 {diff:.6g}"),
                    clip_id=clip.clip_id, tier=tier.name, step=step,
                    payload={"max_diff": diff}))
            # 分歧段最后一步是共享锚点（按设计与文生相等），只校验
            # 锚点之前的替代步必须真正分歧。
            if step < replace - 1 and diff == 0.0 and replace > 0:
                failures.append(Failure(
                    code=ErrorCode.E_PATH_DIVERGENCE,
                    message=f"分歧段替代步 {step} 两路径潜变量不应相同",
                    clip_id=clip.clip_id, tier=tier.name, step=step))
        return failures

    def _upgrade_tier(self, clip: Clip, tier: Tier, prev_tier: Tier,
                      prev_text: Dict[int, np.ndarray],
                      prev_image: Dict[int, np.ndarray], mode: str,
                      clip_count: int
                      ) -> Tuple[Dict[int, np.ndarray], Dict[int, np.ndarray]]:
        motion = dn.motion_matrix(clip)
        checkpoint = dn.checkpoint_for_tier(
            self.config.tiers.index(tier),
            self.config.img2img_replace_steps,
            self.config.denoise_steps)
        text_seed = dn.upsample_latent(
            prev_text[checkpoint], prev_tier, tier)
        image_seed = dn.upsample_latent(
            prev_image[checkpoint], prev_tier, tier)
        remaining = list(range(checkpoint + 1, self.config.denoise_steps))
        text_states = {checkpoint: text_seed}
        image_states = {checkpoint: image_seed.copy()}
        text_states = self._run_steps(
            text_seed, remaining, "shared", tier, clip, motion,
            mode, clip_count, text_states)
        image_states = self._run_steps(
            image_seed, remaining, "shared", tier, clip, motion,
            mode, clip_count, image_states)
        return text_states, image_states

    # ---------------- 局部潜变量补丁 ----------------
    def _apply_patches(self, clip_id: str, tier_name: str,
                       states: Dict[int, np.ndarray],
                       patches: Sequence[LatentPatch]) -> None:
        for patch in patches:
            if patch.clip_id != clip_id or patch.tier != tier_name:
                continue
            states[patch.step][patch.frame, patch.channel] += np.float32(patch.delta)

    def _persist_states(self, store: SnapshotStore, clip_id: str,
                        tier_name: str, path_name: str,
                        states: Dict[int, np.ndarray]) -> None:
        base = os.path.join("stages", "denoise", clip_id, tier_name, path_name)
        for step, array in sorted(states.items()):
            store.save_array(os.path.join(base, f"step_{step:04d}.raw"), array)

    def _decode_tier(self, states: Dict[int, np.ndarray], clip: Clip,
                     tier: Tier, mode: str) -> Dict[int, np.ndarray]:
        last_step = max(states)
        latent = states[last_step]
        if mode == SERIAL:
            frame_order = [[index] for index in range(self.config.frames)]
        else:
            frame_order = [list(range(self.config.frames))]
        frames: Dict[int, np.ndarray] = {}
        for group in frame_order:
            for frame_index in group:
                frames[frame_index] = dn.decode_frame(
                    latent[frame_index], self.config, tier, clip.clip_id)
        return frames

    def _render_clip(self, clip_index: int, clip: Clip,
                     selected: List[TierPlan], mode: str,
                     store: SnapshotStore, patches: Sequence[LatentPatch],
                     prompt_override: Optional[str]
                     ) -> ClipResult:
        clip_id = clip.clip_id
        motion = dn.motion_matrix(clip)
        prompt = prompt_override or clip.prompt
        failures: List[Failure] = []
        init_artifacts: Dict[str, Dict[str, InitArtifact]] = {}
        tier_reuse: List[Dict[str, Any]] = []
        decoded: Dict[str, Dict[str, np.ndarray]] = {}
        steps_done: Dict[Tuple[str, str], int] = {}
        text_by_tier: Dict[str, Dict[int, np.ndarray]] = {}
        image_by_tier: Dict[str, Dict[int, np.ndarray]] = {}

        for plan in selected:
            tier = self.config.tier(plan.tier)
            artifacts = self._stage_init(clip, tier, prompt)
            embed_fp = artifacts["_embed_fp"]
            init_artifacts[plan.tier] = {
                TEXT: artifacts[TEXT], IMAGE: artifacts[IMAGE]}
            self._persist_init(store, clip_id, plan.tier, artifacts)
            store.commit_manifest(f"init:{clip_id}:{plan.tier}")

            if plan.tier_index == 0:
                text_states, image_states, segment_failures = self._tier_zero(
                    clip, tier, artifacts, mode, len(selected))
                reused_steps = 0
                source_tier = None
            else:
                prev_name = self.config.tiers[plan.tier_index - 1].name
                prev_tier = self.config.tier(prev_name)
                text_states, image_states = self._upgrade_tier(
                    clip, tier, prev_tier,
                    text_by_tier[prev_name], image_by_tier[prev_name],
                    mode, len(selected))
                segment_failures = self._verify_segments(
                    clip, tier, text_states, image_states)
                reused_steps = plan.reused_steps
                source_tier = prev_name

            self._apply_patches(clip_id, plan.tier, text_states, patches)
            self._apply_patches(clip_id, plan.tier, image_states, patches)
            failures.extend(segment_failures)

            self._persist_states(store, clip_id, plan.tier, TEXT, text_states)
            self._persist_states(store, clip_id, plan.tier, IMAGE, image_states)
            store.commit_manifest(f"denoise:{clip_id}:{plan.tier}")
            text_by_tier[plan.tier] = text_states
            image_by_tier[plan.tier] = image_states
            steps_done[(plan.tier, TEXT)] = len(text_states)
            steps_done[(plan.tier, IMAGE)] = len(image_states)

            tier_reuse.append({
                "tier": plan.tier,
                "source_tier": source_tier,
                "checkpoint_step": plan.checkpoint_step,
                "reused_steps": reused_steps,
                "recomputed_steps": plan.new_steps,
                "upsampled": plan.tier_index > 0,
            })

            frames = self._decode_tier(text_states, clip, tier, mode)
            decoded[plan.tier] = frames
            for frame_index, rgb in sorted(frames.items()):
                rel = os.path.join("frames", clip_id, plan.tier,
                                   f"text_{frame_index:04d}.png")
                self._write_png(store, rel, rgb)
            image_frames = self._decode_tier(image_states, clip, tier, mode)
            for frame_index, rgb in sorted(image_frames.items()):
                rel = os.path.join("frames", clip_id, plan.tier,
                                   f"image_{frame_index:04d}.png")
                self._write_png(store, rel, rgb)
            store.commit_manifest(f"decode:{clip_id}:{plan.tier}")

        task_keys: List[Tuple[int, int, int, int, int]] = []
        for tier_index, plan in enumerate(selected):
            for path_index, _path_name in enumerate((TEXT, IMAGE)):
                for step in range(self.config.denoise_steps):
                    for frame in range(self.config.frames):
                        task_keys.append(_stable_key(
                            clip_index, tier_index, path_index, step, frame))
        divergent, shared = dn.shared_segment(
            self.config.img2img_replace_steps, self.config.denoise_steps)
        segments = {
            "divergent_steps": divergent,
            "shared_steps": shared,
            "replace_steps": self.config.img2img_replace_steps,
        }
        return ClipResult(
            clip_id=clip_id, embeds_fingerprint=embed_fp,
            init_artifacts=init_artifacts, tier_reuse=tier_reuse,
            segments=segments, divergence=failures, decoded=decoded,
            steps_done=steps_done,
            order_key=(clip_index,),
            task_keys=task_keys)

    @staticmethod
    def _write_png(store: SnapshotStore, rel: str, rgb: np.ndarray) -> None:
        from io import BytesIO
        buffer = BytesIO()
        Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
        store.atomic_bytes(rel, buffer.getvalue())

    def run(self, request: RenderRequest) -> Dict[str, Any]:
        config_hash = self.config.config_hash()
        store = SnapshotStore(request.output_dir)
        store.begin_run(config_hash, list(self.config.tier_names))
        self.decision = schedule(self.config, len(request.clips))

        mode = self.decision.batch_mode
        selected = self.decision.selected_tiers
        results: List[ClipResult] = []
        failures: List[Failure] = list(request.load_failures)
        failures.extend(self.decision.failures)
        terminated_gracefully = False
        cache_keys = {
            clip.clip_id: self._clip_cache_key(
                self.config, clip,
                request.prompt_overrides.get(clip.clip_id) or clip.prompt,
                request.latent_patches)
            for clip in request.clips}

        if not selected:
            # 预算为零等情形：没有可渲染档位，保留清单与中间状态判定。
            store.commit_manifest("no-budget")
        # 增量复用：命中缓存的片段直接从已完整替换的快照恢复，不重新计算。
        pending_clips: List[Tuple[int, Clip]] = []
        stop_requested = request.stop_event is not None and request.stop_event.is_set()
        for index, clip in enumerate(request.clips):
            if stop_requested:
                failures.append(Failure(
                    code=ErrorCode.E_STAGE_FAILED,
                    message="收到退出信号，未派发片段保留为可继续中间状态",
                    clip_id=clip.clip_id))
                continue
            reused = self._try_reuse(index, clip, selected, store,
                                     cache_keys[clip.clip_id])
            if reused is not None:
                results.append(reused)
            else:
                pending_clips.append((index, clip))

        if not selected:
            pass
        elif mode == CROSS_CLIP:
            # 跨片段合并：同一工作函数在线程池内处理不同片段，结果仍按键归位。
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
                futures = {}
                for index, clip in pending_clips:
                    key = _stable_key(index, 0, 0, 0, 0)
                    future = pool.submit(
                        self._safe_render, index, clip, selected, mode,
                        store, request.latent_patches,
                        request.prompt_overrides.get(clip.clip_id),
                        cache_keys[clip.clip_id])
                    futures[future] = key
                ordered = []
                for future in futures:
                    result, clip_failures = future.result()
                    ordered.append((futures[future], result, clip_failures))
                for _key, result, clip_failures in sorted(
                        ordered, key=lambda item: item[0]):
                    if result is not None:
                        results.append(result)
                    failures.extend(clip_failures)
        else:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
                futures = {}
                for index, clip in pending_clips:
                    key = _stable_key(index, 0, 0, 0, 0)
                    future = pool.submit(
                        self._safe_render, index, clip, selected, mode,
                        store, request.latent_patches,
                        request.prompt_overrides.get(clip.clip_id),
                        cache_keys[clip.clip_id])
                    futures[future] = key
                ordered = []
                for future in futures:
                    result, clip_failures = future.result()
                    ordered.append((futures[future], result, clip_failures))
                for _key, result, clip_failures in sorted(
                        ordered, key=lambda item: item[0]):
                    if result is not None:
                        results.append(result)
                    failures.extend(clip_failures)

        version_before = store.version
        if request.stop_event is not None and request.stop_event.is_set():
            store.commit_manifest("graceful-shutdown")
            terminated_gracefully = True
        elif not selected:
            store.commit_manifest("run-complete-no-budget")
        elif pending_clips:
            # 至少有片段真正重算，提交一次运行级清单。
            store.commit_manifest("run-complete")
        # 全部片段命中缓存时不产生新阶段替换，版本号保持不变。

        return {
            "store": store,
            "results": results,
            "failures": sorted(failures, key=_failure_sort_key),
            "decision": self.decision,
            "graceful_shutdown": terminated_gracefully,
            "manifest_version": store.version,
        }

    def _safe_render(self, clip_index: int, clip: Clip,
                     selected: List[TierPlan], mode: str,
                     store: SnapshotStore, patches: Sequence[LatentPatch],
                     prompt_override: Optional[str],
                     cache_key: str = ""
                     ) -> Tuple[Optional[ClipResult], List[Failure]]:
        try:
            result = self._render_clip(
                clip_index, clip, selected, mode, store, patches,
                prompt_override)
            state = {
                "clip_id": clip.clip_id,
                "status": "complete",
                "tiers": [plan.tier for plan in selected],
                "steps_cursor": {
                    f"{key[0]}:{key[1]}": value
                    for key, value in result.steps_done.items()},
                "embed_fingerprint": result.embeds_fingerprint,
                "tier_reuse": result.tier_reuse,
                "cache_key": cache_key,
            }
            store.update_clip(clip.clip_id, state)
            store.commit_manifest(f"clip-complete:{clip.clip_id}")
            return result, result.divergence
        except Exception as exc:  # 单阶段/单片段异常隔离，不阻断其余片段
            failure = Failure(
                code=ErrorCode.E_STAGE_FAILED,
                message=f"片段渲染失败: {exc}",
                clip_id=clip.clip_id,
                exception=type(exc).__name__)
            store.update_clip(clip.clip_id, {
                "clip_id": clip.clip_id,
                "status": "failed",
                "exception": type(exc).__name__,
            })
            store.commit_manifest(f"clip-failed:{clip.clip_id}")
            return None, [failure]

    # ---------------- 增量快照复用 ----------------
    def _clip_cache_key(self, config: "Config", clip: Clip,
                        prompt: str, patches: Sequence[LatentPatch]) -> str:
        from .hashing import stable_hash
        embed_fp = fingerprint_array(self.encoder.embed(prompt))
        patch_fp = stable_hash("patches",
                               [patch.fingerprint() for patch in patches
                                if patch.clip_id == clip.clip_id])
        return stable_hash(
            "clip-input", config.config_hash(), clip.clip_id,
            embed_fp, clip.init_image_fingerprints, patch_fp)

    def _try_reuse(self, clip_index: int, clip: Clip,
                   selected: List[TierPlan], store: SnapshotStore,
                   cache_key: str) -> Optional[ClipResult]:
        state = store.clip_state(clip.clip_id)
        if state.get("status") != "complete" or state.get("cache_key") != cache_key:
            return None
        try:
            init_artifacts: Dict[str, Dict[str, InitArtifact]] = {}
            tier_reuse: List[Dict[str, Any]] = []
            decoded: Dict[str, Dict[str, np.ndarray]] = {}
            steps_done: Dict[Tuple[str, str], int] = {}
            for plan in selected:
                tier = self.config.tier(plan.tier)
                tier_init: Dict[str, InitArtifact] = {}
                for path_name in (TEXT, IMAGE):
                    rel = os.path.join("stages", "init", clip.clip_id,
                                       plan.tier, f"{path_name}.raw")
                    latent = store.load_array(rel, (
                        self.config.frames, self.config.latent_channels,
                        tier.latent_shape[1], tier.latent_shape[2]))
                    manifest_rel = os.path.join(
                        "stages", "init", clip.clip_id, plan.tier,
                        f"{path_name}.manifest.json")
                    import json as _json
                    with open(os.path.join(store.root, manifest_rel),
                              "r", encoding="utf-8") as handle:
                        manifest = _json.load(handle)
                    tier_init[path_name] = InitArtifact(
                        path=path_name, tier=plan.tier, latent=latent,
                        fingerprint=manifest["fingerprint"],
                        dependencies=manifest["dependencies"])
                init_artifacts[plan.tier] = tier_init
                frames: Dict[int, np.ndarray] = {}
                for frame_index in range(self.config.frames):
                    frame_rel = os.path.join(
                        "frames", clip.clip_id, plan.tier,
                        f"text_{frame_index:04d}.png")
                    from PIL import Image as _Image
                    with _Image.open(os.path.join(store.root, frame_rel)) as img:
                        frames[frame_index] = np.asarray(
                            img.convert("RGB"), dtype=np.uint8)
                decoded[plan.tier] = frames
                steps_done[(plan.tier, TEXT)] = plan.new_steps + plan.reused_steps
                steps_done[(plan.tier, IMAGE)] = plan.new_steps + plan.reused_steps
                tier_reuse.append({
                    "tier": plan.tier,
                    "source_tier": (None if plan.tier_index == 0
                                    else self.config.tiers[plan.tier_index - 1].name),
                    "checkpoint_step": plan.checkpoint_step,
                    "reused_steps": plan.reused_steps,
                    "recomputed_steps": plan.new_steps,
                    "upsampled": plan.tier_index > 0,
                    "snapshot_hit": True,
                })
            divergent, shared = dn.shared_segment(
                self.config.img2img_replace_steps,
                self.config.denoise_steps)
            return ClipResult(
                clip_id=clip.clip_id,
                embeds_fingerprint=state.get("embed_fingerprint", ""),
                init_artifacts=init_artifacts, tier_reuse=tier_reuse,
                segments={"divergent_steps": divergent,
                          "shared_steps": shared,
                          "replace_steps": self.config.img2img_replace_steps},
                divergence=[], decoded=decoded, steps_done=steps_done,
                order_key=(clip_index,),
                task_keys=[_stable_key(clip_index, tier_index, path_index,
                                       step, frame)
                           for tier_index in range(len(selected))
                           for path_index in range(2)
                           for step in range(self.config.denoise_steps)
                           for frame in range(self.config.frames)])
        except Exception:
            # 任何不完整/损坏产物都回退到重算，绝不基于半替换状态恢复。
            return None

    def preview_first_frame(self, request: RenderRequest) -> Dict[str, Any]:
        """需求 5：首帧预算内最低档预览，不做高档位阻塞计算。"""
        plan = preview_plan(self.config, len(request.clips) or 1)
        store = SnapshotStore(request.output_dir)
        store.begin_run(self.config.config_hash(),
                        [self.config.tiers[0].name])
        response: Dict[str, Any] = {"preview": plan, "frames": {}}
        if not plan["eligible"]:
            store.commit_manifest("preview-pending")
            response["code"] = ErrorCode.E_PREVIEW_BUDGET_PENDING
            response["manifest_version"] = store.version
            return response
        if not request.clips:
            store.commit_manifest("preview-empty")
            response["manifest_version"] = store.version
            return response
        clip = request.clips[0]
        tier = self.config.tiers[0]
        artifacts = self._stage_init(clip, tier, clip.prompt)
        self._persist_init(store, clip.clip_id, tier.name, artifacts)
        motion = dn.motion_matrix(clip)
        states = self._run_steps(
            artifacts[TEXT].latent, range(self.config.denoise_steps),
            "shared", tier, clip, motion, SERIAL, 1)
        frame = dn.decode_frame(states[self.config.denoise_steps - 1][0],
                                self.config, tier, clip.clip_id)
        rel = os.path.join("frames", clip.clip_id, tier.name,
                           "preview_text_0000.png")
        self._write_png(store, rel, frame)
        store.commit_manifest("preview-ready")
        response["frames"][clip.clip_id] = os.path.join(request.output_dir, rel)
        response["progress"] = {
            "clips_total": len(request.clips),
            "clips_previewed": 1,
            "tier": tier.name,
            "frame_index": 0,
        }
        response["manifest_version"] = store.version
        return response


def _failure_sort_key(failure: Failure
                      ) -> Tuple[str, str, str, int, int, str]:
    return (
        failure.code,
        failure.clip_id or "",
        failure.tier or "",
        failure.step if failure.step is not None else -1,
        failure.frame if failure.frame is not None else -1,
        failure.message,
    )


