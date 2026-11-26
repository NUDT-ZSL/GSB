from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AuditConfig:
    sample_rate: int = 16_000
    frame_ms: int = 20
    hop_ms: int = 10
    # Raw int16 RMS thresholds. Silence in the fixed sample is below 100.
    vad_start_rms_q: int = 900
    vad_end_rms_q: int = 450
    # ZCR transition counts over 319 adjacent pairs.
    zcr_low_max: int = 5
    zcr_mid_max: int = 8
    shift_confirm_frames: int = 3
    # Business thresholds, all represented as integer milliseconds.
    rapid_switch_ms: int = 900
    silence_long_ms: int = 1_200
    min_stable_turn_ms: int = 250
    shift_cooldown_ms: int = 300
    preempt_energy_ratio_q: int = 140  # ratio * 100: 1.40
    shift_energy_ratio_q: int = 125
    # Identity clustering threshold in normalized log-pitch distance.
    identity_distance_q: int = 18  # distance * 100
    query_precision_ms: int = 10

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def hop_samples(self) -> int:
        return self.sample_rate * self.hop_ms // 1000

    @property
    def samples_per_ms(self) -> int:
        return self.sample_rate // 1000

    def frame_start_sample(self, frame_index: int) -> int:
        return frame_index * self.hop_samples

    def frame_center_ms(self, frame_index: int) -> int:
        return frame_index * self.hop_ms + self.frame_ms // 2

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["frame_samples"] = self.frame_samples
        data["hop_samples"] = self.hop_samples
        return data


DEFAULT_CONFIG = AuditConfig()
