"""Bidirectional policy version conversion with a loss side-channel.

Upgrade v1 -> v2 is exact: milli-unit integers map to floats through an exact
conversion table (milli / 1000), the 32-bit seed is zero-extended to 64 bits,
and greedy/sampling decoder semantics are preserved.

Downgrade v2 -> v1 is lossy: temperature is folded onto a configurable
quantization grid, top_p is clamped to [0, 1000] milli, a 64-bit seed outside
the 32-bit range is truncated or rejected per configuration, and the beam
decoder degrades to sampling with beam_width / length_alpha written to a
bypass metadata file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import E_SEED_WIDTH, WorkbenchError
from .policy import UINT32_MAX, Policy

LOSS_PRECISION = "precision"
LOSS_CLAMP = "clamp"
LOSS_SEED_WIDTH = "seed_width"
LOSS_CAPABILITY = "capability"


@dataclass(frozen=True)
class LossEntry:
    field: str
    old: object
    new: object
    loss_class: str

    def to_dict(self):
        return {
            "field": self.field,
            "old": self.old,
            "new": self.new,
            "loss_class": self.loss_class,
        }


def _sorted_losses(losses):
    # Deterministic loss manifest: identical policy converts to an identical,
    # field-by-field identical loss list on every run.
    return sorted(losses, key=lambda e: (e.field, e.loss_class, str(e.old), str(e.new)))


def upgrade_v1_to_v2(policy):
    """Exact upgrade. Returns (policy, []) -- the conversion is lossless."""
    if policy.source_version != "v1":
        raise WorkbenchError("E_CONVERT", "upgrade expects a v1 policy")
    upgraded = policy.with_fields(source_version="v2")
    return upgraded, []


def quantize_temperature(temperature, quantum):
    """Fold a float temperature onto the configured quantization grid (milli)."""
    steps = round(temperature / quantum)
    quantized = steps * quantum
    milli = int(round(quantized * 1000))
    return milli


def downgrade_v2_to_v1(policy, temp_quantum=0.001, seed_width_rule="truncate"):
    """Lossy downgrade. Returns (policy, sidecar, losses).

    sidecar: dict with beam metadata when a beam decoder is degraded, else {}.
    losses: deterministic list of LossEntry.
    """
    if policy.source_version != "v2":
        raise WorkbenchError("E_CONVERT", "downgrade expects a v2 policy")
    if seed_width_rule not in ("truncate", "error"):
        raise WorkbenchError("E_CONVERT", f"unknown seed_width_rule {seed_width_rule!r}")

    losses = []
    sidecar = {}

    temp_milli = quantize_temperature(policy.temperature, temp_quantum)
    new_temp = temp_milli / 1000.0
    if new_temp != policy.temperature:
        losses.append(
            LossEntry("temperature", policy.temperature, new_temp, LOSS_PRECISION)
        )

    top_p_milli = int(round(policy.top_p * 1000))
    clamped = min(1000, max(0, top_p_milli))
    if clamped != top_p_milli:
        losses.append(LossEntry("top_p", policy.top_p, clamped / 1000.0, LOSS_CLAMP))
    elif clamped / 1000.0 != policy.top_p:
        losses.append(
            LossEntry("top_p", policy.top_p, clamped / 1000.0, LOSS_PRECISION)
        )

    rp_milli = int(round(policy.repetition_penalty * 1000))
    new_rp = rp_milli / 1000.0
    if new_rp != policy.repetition_penalty:
        losses.append(
            LossEntry(
                "repetition_penalty", policy.repetition_penalty, new_rp, LOSS_PRECISION
            )
        )

    seed = policy.seed
    if seed > UINT32_MAX:
        if seed_width_rule == "error":
            raise WorkbenchError(
                E_SEED_WIDTH,
                f"seed {seed} exceeds 32-bit range and seed_width_rule=error",
            )
        new_seed = seed & UINT32_MAX
        losses.append(LossEntry("seed", seed, new_seed, LOSS_SEED_WIDTH))
        seed = new_seed

    decoder_type = policy.decoder_type
    beam_width = policy.beam_width
    length_alpha = policy.length_alpha
    if decoder_type == "beam":
        decoder_type = "sampling"
        sidecar = {
            "beam_width": policy.beam_width,
            "length_alpha": policy.length_alpha,
        }
        losses.append(
            LossEntry("decoder_type", "beam", "sampling", LOSS_CAPABILITY)
        )
        losses.append(
            LossEntry("beam_width", policy.beam_width, None, LOSS_CAPABILITY)
        )
        losses.append(
            LossEntry("length_alpha", policy.length_alpha, None, LOSS_CAPABILITY)
        )
        beam_width = 1
        length_alpha = 0.0

    downgraded = Policy(
        decoder_type=decoder_type,
        temperature=new_temp,
        top_k=policy.top_k,
        top_p=clamped / 1000.0,
        repetition_penalty=new_rp,
        seed=seed,
        beam_width=beam_width,
        length_alpha=length_alpha,
        source_version="v1",
        raw_fields={},
    )
    return downgraded, sidecar, _sorted_losses(losses)


def convert_policy(policy, target_version, temp_quantum=0.001, seed_width_rule="truncate"):
    """Convert a policy to the target exchange version.

    Returns (policy, sidecar, losses). Same-version requests are pass-through.
    """
    if target_version == policy.source_version:
        return policy, {}, []
    if target_version == "v2":
        upgraded, _ = upgrade_v1_to_v2(policy)
        return upgraded, {}, []
    if target_version == "v1":
        return downgrade_v2_to_v1(policy, temp_quantum, seed_width_rule)
    raise WorkbenchError("E_CONVERT", f"unknown target version {target_version!r}")
