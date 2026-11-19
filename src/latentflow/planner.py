from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import checksum_payload
from .config import VideoConfig
from .decoder import DeterministicDecoder
from .errors import BusinessError, ErrorCode


@dataclass(frozen=True)
class DecodePlan:
    requested_block_size: int
    block_size: int
    frames: int
    num_blocks: int
    latency_budget_ms: float
    estimated_first_block_ms: float
    estimate_basis: dict[str, Any]
    chunk_memory_bytes: int
    consistency_policy: dict[str, Any]
    arbitration: list[dict[str, Any]]
    degradations: list[str]
    context_setup_ms: float

    def to_dict(self) -> dict[str, Any]:
        body = {
            "requested_block_size": self.requested_block_size,
            "selected_block_size": self.block_size,
            "frames": self.frames,
            "num_blocks": self.num_blocks,
            "latency_budget_ms": self.latency_budget_ms,
            "estimated_first_block_ms": round(self.estimated_first_block_ms, 6),
            "estimate_basis": self.estimate_basis,
            "chunk_memory_bytes": self.chunk_memory_bytes,
            "consistency_policy": self.consistency_policy,
            "arbitration": self.arbitration,
            "degradations": self.degradations,
            "context_setup_ms": round(self.context_setup_ms, 6),
        }
        return {**body, "plan_checksum": checksum_payload(body)}


class DecodePlanner:
    """
    Explicit arbitration order:
    1. Byte consistency is a hard invariant (global statistics, no relaxation).
    2. Resource limits are hard; unsafe chunks are reduced or rejected.
    3. The largest requested-compatible chunk that meets the latency budget wins.
    4. Smaller chunks / more boundaries strengthen boundary verification.
    """

    def __init__(self, config: VideoConfig, decoder: DeterministicDecoder) -> None:
        self.config = config
        self.decoder = decoder

    def plan(self, requested_block_size: int | None = None) -> DecodePlan:
        requested = requested_block_size or self.config.block_size
        if requested < 1:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "块大小必须为正整数",
                {"block_size": requested},
            )
        requested = min(requested, self.config.frames)
        arbitration: list[dict[str, Any]] = []
        degradations: list[str] = []

        max_chunk_frames = self.config.max_chunk_bytes // (
            self.config.width * self.config.height * 3
        )
        memory_cap = max(1, max_chunk_frames)
        candidate = min(requested, memory_cap, self.config.frames)
        if candidate < requested:
            degradations.append("block_size_reduced_for_memory")
            arbitration.append(
                {
                    "rule": "resource_limit",
                    "decision": "reduce_block_size",
                    "from": requested,
                    "to": candidate,
                    "reason": "单块 RGB 驻留内存超过 max_chunk_bytes",
                }
            )
        else:
            arbitration.append(
                {
                    "rule": "resource_limit",
                    "decision": "accept",
                    "block_size": candidate,
                    "reason": "单块驻留内存处于上限内",
                }
            )

        self.decoder.ensure_context()
        selected: tuple[int, Any] | None = None
        rejected: list[dict[str, Any]] = []
        for size in range(candidate, 0, -1):
            measured_frames = min(size, self.config.frames)
            probe = self.decoder.decode_range(0, measured_frames, block_index=0)
            measured = probe.timing.total_ms
            if measured <= self.config.latency_budget_ms:
                selected = (size, probe)
                if size < candidate:
                    arbitration.append(
                        {
                            "rule": "latency_budget",
                            "decision": "reduce_block_size",
                            "from": candidate,
                            "to": size,
                            "reason": "较大块的首块实测延迟超过预算",
                        }
                    )
                break
            rejected.append(
                {
                    "block_size": size,
                    "measured_first_block_ms": round(measured, 6),
                }
            )

        if selected is None:
            raise BusinessError(
                ErrorCode.E_LATENCY_BUDGET,
                "任何合规块大小都无法满足首块延迟预算",
                {
                    "stage": "decode_planning",
                    "completed_stages": [
                        "prompt_hash",
                        "seed_initial_latent",
                        "denoise",
                    ],
                    "decoded_blocks": 0,
                    "latency_budget_ms": self.config.latency_budget_ms,
                    "rejected_candidates": rejected[-8:],
                    "context_setup_ms": round(self.decoder.context_setup_ms, 6),
                },
            )

        block_size, probe = selected
        num_blocks = (self.config.frames + block_size - 1) // block_size
        if block_size < 4 or num_blocks > 8:
            verification = "exhaustive_all_boundaries_and_frames"
            risk = "high_boundary_density"
        elif num_blocks > 4:
            verification = "reinforced_all_boundaries"
            risk = "moderate_boundary_density"
        else:
            verification = "standard_first_boundary_plus_full_pixel_scan"
            risk = "low_boundary_density"
        arbitration.append(
            {
                "rule": "consistency",
                "decision": "never_trade_away_byte_equality",
                "verification_strength": verification,
                "risk": risk,
                "reason": "所有块共享整段统计量；边界增多时只提高校验强度",
            }
        )

        stage_total = sum(probe.timing.stage_ms.values())
        estimate_basis = {
            "method": "local_decode_loop_probe",
            "measured_block_frames": probe.frame_count,
            "decode_stages": probe.timing.decode_stages,
            "measured_total_ms": round(probe.timing.total_ms, 6),
            "measured_stage_ms": {
                key: round(value, 6) for key, value in probe.timing.stage_ms.items()
            },
            "measured_ms_per_frame_stage": round(
                stage_total / max(1, probe.frame_count * probe.timing.decode_stages),
                9,
            ),
            "estimated_frames": min(block_size, self.config.frames),
            "formula": "local probe total latency for the selected first block",
        }
        return DecodePlan(
            requested_block_size=requested,
            block_size=block_size,
            frames=self.config.frames,
            num_blocks=num_blocks,
            latency_budget_ms=self.config.latency_budget_ms,
            estimated_first_block_ms=probe.timing.total_ms,
            estimate_basis=estimate_basis,
            chunk_memory_bytes=min(block_size, self.config.frames)
            * self.config.width
            * self.config.height
            * 3,
            consistency_policy={
                "normalization_scope": "entire_latent_sequence",
                "temporal_blend_source": "previous_latent_frame",
                "quantization": "floor_half_up_uint8",
                "anchor_scope": "frame_index_based",
                "verification": verification,
                "byte_equality_is_hard_invariant": True,
            },
            arbitration=arbitration,
            degradations=degradations,
            context_setup_ms=self.decoder.context_setup_ms,
        )
