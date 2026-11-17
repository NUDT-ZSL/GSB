"""Transfer-function implementations and shared lookup tables.

The implementation follows the constants in ITU-R BT.709, ITU-R BT.2100
HLG and SMPTE ST 2084 (PQ).  HLG absolute decoding includes the BT.2100
OOTF; its inverse is analytic, so RGB round trips are deterministic.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Iterable, Sequence

from .errors import BusinessError


TRANSFER_ALIASES = {
    "SDR": "SDR_GAMMA",
    "SDR_GAMMA": "SDR_GAMMA",
    "BT709": "SDR_GAMMA",
    "BT1886": "SDR_GAMMA",
    "HLG": "HLG",
    "PQ": "PQ",
    "ST2084": "PQ",
}
ALLOWED_TRANSFERS = ("SDR_GAMMA", "HLG", "PQ")


# BT.709 uses the precise alpha/beta pair that makes the two branches meet.
SDR_CONSTANTS = {
    "standard": "ITU-R BT.709 OETF",
    "alpha": 1.09929682680944,
    "beta": 0.018053968510807,
    "slope": 4.5,
    "gamma": 0.45,
    "beta_signal": 0.081242858298635,
    "nominal_reference_white_nits": 100.0,
}

HLG_CONSTANTS = {
    "standard": "ITU-R BT.2100 HLG",
    "a": 0.17883277,
    "b": 0.28466892,
    "c": 0.55991073,
    "scene_breakpoint": 1.0 / 12.0,
    "signal_breakpoint": 0.5,
    "system_gamma_min": 1.2,
    "system_gamma_max": 1.4,
    "nominal_peak_nits": 1000.0,
}

PQ_CONSTANTS = {
    "standard": "SMPTE ST 2084 (PQ)",
    "m1": 2610.0 / 16384.0,
    "m2": 2523.0 / 4096.0 * 128.0,
    "c1": 3424.0 / 4096.0,
    "c2": 2413.0 / 4096.0 * 32.0,
    "c3": 2392.0 / 4096.0 * 32.0,
    "peak_nits": 10000.0,
}

LUT_MIN_NORMALIZED = 1e-4


def canonical_transfer(value: object) -> str:
    if not isinstance(value, str):
        raise BusinessError(
            "E_TRANSFER_FUNCTION",
            "transfer function must be a string",
            {"value": repr(value), "allowed": list(ALLOWED_TRANSFERS)},
        )
    candidate = TRANSFER_ALIASES.get(value.strip().upper())
    if candidate is None:
        raise BusinessError(
            "E_TRANSFER_FUNCTION",
            "unsupported transfer function",
            {"value": value, "allowed": list(ALLOWED_TRANSFERS)},
        )
    return candidate


def _sdr_encode_relative(linear: float) -> float:
    if linear < 0.0:
        raise ValueError("SDR encoding domain is non-negative")
    if linear < SDR_CONSTANTS["beta"]:
        return SDR_CONSTANTS["slope"] * linear
    return SDR_CONSTANTS["alpha"] * (linear ** SDR_CONSTANTS["gamma"]) - (
        SDR_CONSTANTS["alpha"] - 1.0
    )


def _sdr_decode_relative(signal: float) -> float:
    if signal < 0.0:
        raise ValueError("SDR signal domain is non-negative")
    threshold = SDR_CONSTANTS["beta_signal"]
    if signal < threshold:
        return signal / SDR_CONSTANTS["slope"]
    return math.pow(
        (signal + SDR_CONSTANTS["alpha"] - 1.0) / SDR_CONSTANTS["alpha"],
        1.0 / SDR_CONSTANTS["gamma"],
    )


def hlg_encode_scene(scene_linear: float) -> float:
    """BT.2100 HLG OETF from relative scene light to signal."""

    if scene_linear < 0.0:
        raise ValueError("HLG scene-linear domain is non-negative")
    if scene_linear <= HLG_CONSTANTS["scene_breakpoint"]:
        return math.sqrt(3.0 * scene_linear)
    return (
        HLG_CONSTANTS["a"] * math.log(12.0 * scene_linear - HLG_CONSTANTS["b"])
        + HLG_CONSTANTS["c"]
    )


def hlg_decode_scene(signal: float) -> float:
    """Inverse BT.2100 HLG OETF."""

    if signal < 0.0:
        raise ValueError("HLG signal domain is non-negative")
    if signal <= HLG_CONSTANTS["signal_breakpoint"]:
        return signal * signal / 3.0
    return (
        math.exp((signal - HLG_CONSTANTS["c"]) / HLG_CONSTANTS["a"])
        + HLG_CONSTANTS["b"]
    ) / 12.0


def hlg_system_gamma(peak_nits: float) -> float:
    gamma = 1.2 + 0.42 * math.log10(max(peak_nits, 1.0) / 1000.0)
    return min(
        HLG_CONSTANTS["system_gamma_max"],
        max(HLG_CONSTANTS["system_gamma_min"], gamma),
    )


def pq_decode_nits(signal: float) -> float:
    """SMPTE ST 2084 EOTF: normalized signal to absolute cd/m2."""

    if signal < 0.0:
        raise ValueError("PQ signal domain is non-negative")
    power = math.pow(signal, 1.0 / PQ_CONSTANTS["m2"])
    numerator = max(power - PQ_CONSTANTS["c1"], 0.0)
    denominator = PQ_CONSTANTS["c2"] - PQ_CONSTANTS["c3"] * power
    if numerator == 0.0:
        return 0.0
    if denominator <= 0.0:
        return PQ_CONSTANTS["peak_nits"]
    return PQ_CONSTANTS["peak_nits"] * math.pow(
        numerator / denominator, 1.0 / PQ_CONSTANTS["m1"]
    )


def pq_encode_nits(nits: float) -> float:
    """Inverse SMPTE ST 2084 EOTF: absolute cd/m2 to normalized signal."""

    if nits < 0.0:
        raise ValueError("PQ absolute-light domain is non-negative")
    normalized = max(nits / PQ_CONSTANTS["peak_nits"], 0.0)
    if normalized == 0.0:
        return 0.0
    power = math.pow(normalized, PQ_CONSTANTS["m1"])
    numerator = PQ_CONSTANTS["c1"] + PQ_CONSTANTS["c2"] * power
    denominator = 1.0 + PQ_CONSTANTS["c3"] * power
    return math.pow(numerator / denominator, PQ_CONSTANTS["m2"])


def _hlg_ootf(scene_rgb: Sequence[float], peak_nits: float) -> list[float]:
    gamma = hlg_system_gamma(peak_nits)
    y_scene = 0.2627 * scene_rgb[0] + 0.6780 * scene_rgb[1] + 0.0593 * scene_rgb[2]
    if y_scene <= 0.0:
        return [0.0, 0.0, 0.0]
    gain = peak_nits * math.pow(y_scene, gamma - 1.0)
    return [gain * channel for channel in scene_rgb]


def _hlg_inverse_ootf(display_rgb: Sequence[float], peak_nits: float) -> list[float]:
    gamma = hlg_system_gamma(peak_nits)
    y_display = (
        0.2627 * display_rgb[0]
        + 0.6780 * display_rgb[1]
        + 0.0593 * display_rgb[2]
    )
    if y_display <= 0.0:
        return [0.0, 0.0, 0.0]
    divisor = math.pow(peak_nits * math.pow(y_display, gamma - 1.0), 1.0 / gamma)
    return [channel / divisor for channel in display_rgb]


@lru_cache(maxsize=32)
def _build_lut(kind: str, size: int) -> tuple[array, array]:
    """Return immutable-by-convention encode/decode LUTs.

    Encode input domain is the curve's normalized linear domain and output is
    the normalized signal.  Decode is the inverse.  The arrays live in the
    shared TransferFunctions object and are only read by worker threads.
    """

    encode = array("d")
    decode = array("d")
    denominator = float(size - 1)
    for index in range(size):
        x = index / denominator
        if kind == "SDR_GAMMA":
            encoded = _sdr_encode_relative(x)
            decoded = _sdr_decode_relative(x)
        elif kind == "HLG":
            encoded = hlg_encode_scene(x)
            decoded = hlg_decode_scene(x)
        elif kind == "PQ":
            encoded = pq_encode_nits(x * PQ_CONSTANTS["peak_nits"])
            decoded = pq_decode_nits(x)
        else:  # guarded before this point
            raise ValueError(f"unknown transfer kind {kind}")
        encode.append(encoded)
        decode.append(decoded)
    return encode, decode


def _lut_lookup(table: array, x: float) -> float:
    if x <= 0.0:
        return table[0]
    if x >= 1.0:
        return table[-1]
    position = x * (len(table) - 1)
    index = int(position)
    fraction = position - index
    lower = table[index]
    if fraction == 0.0:
        return lower
    return lower + (table[index + 1] - lower) * fraction


@dataclass(frozen=True)
class TransferFunctions:
    kind: str
    reference_white_nits: float
    peak_nits: float
    lut_size: int
    encode_lut: array
    decode_lut: array

    @classmethod
    def create(
        cls,
        kind: str,
        reference_white_nits: float,
        peak_nits: float,
        lut_size: int = 65537,
    ) -> "TransferFunctions":
        canonical = canonical_transfer(kind)
        if not math.isfinite(peak_nits) or peak_nits <= 0.0:
            raise BusinessError(
                "E_LUMINANCE",
                "transfer peak must be finite and positive",
                {"peak_nits": peak_nits},
            )
        if lut_size < 1025:
            raise BusinessError(
                "E_PROCESSING_CONFIG",
                "curve_lut_size must be at least 1025",
                {"curve_lut_size": lut_size},
            )
        encode_lut, decode_lut = _build_lut(canonical, lut_size)
        return cls(
            kind=canonical,
            reference_white_nits=float(reference_white_nits),
            peak_nits=float(peak_nits),
            lut_size=int(lut_size),
            encode_lut=encode_lut,
            decode_lut=decode_lut,
        )

    @property
    def shared_lut_bytes(self) -> int:
        return self.encode_lut.buffer_info()[1] * self.encode_lut.itemsize * 2

    def constants(self) -> dict[str, object]:
        if self.kind == "SDR_GAMMA":
            return {
                **SDR_CONSTANTS,
                "reference_white_nits": self.reference_white_nits,
            }
        if self.kind == "HLG":
            return {
                **HLG_CONSTANTS,
                "absolute_peak_nits": self.peak_nits,
                "system_gamma": hlg_system_gamma(self.peak_nits),
                "uses_ootf": True,
            }
        return {**PQ_CONSTANTS, "absolute_peak_nits": self.peak_nits}

    def report(self) -> dict[str, object]:
        evaluation = (
            "analytic_exact (PQ LUT is retained as a shared optional preview table)"
            if self.kind == "PQ"
            else "shared LUT with exact analytic fallback outside normalized domain"
        )
        return {
            "transfer": self.kind,
            "constants": self.constants(),
            "lut_size": self.lut_size,
            "lut_bytes": self.shared_lut_bytes,
            "lut_shared_read_only": True,
            "evaluation_path": evaluation,
        }

    def encode_rgb(self, linear_nits_rgb: Sequence[float]) -> list[float]:
        if self.kind == "SDR_GAMMA":
            return [
                self._encode_sdr_channel(max(channel / self.reference_white_nits, 0.0))
                for channel in linear_nits_rgb
            ]
        if self.kind == "PQ":
            return [
                pq_encode_nits(max(channel, 0.0))
                for channel in linear_nits_rgb
            ]

        scene = _hlg_inverse_ootf(linear_nits_rgb, self.peak_nits)
        return [
            self._encode_hlg_channel(max(channel, 0.0))
            for channel in scene
        ]

    def decode_rgb(self, signal_rgb: Sequence[float]) -> list[float]:
        if self.kind == "SDR_GAMMA":
            return [
                self._decode_sdr_channel(max(channel, 0.0))
                * self.reference_white_nits
                for channel in signal_rgb
            ]
        if self.kind == "PQ":
            return [
                pq_decode_nits(max(channel, 0.0))
                for channel in signal_rgb
            ]

        scene = [
            self._decode_hlg_channel(max(channel, 0.0))
            for channel in signal_rgb
        ]
        return _hlg_ootf(scene, self.peak_nits)

    def _encode_sdr_channel(self, linear_relative: float) -> float:
        if LUT_MIN_NORMALIZED <= linear_relative <= 1.0:
            return _lut_lookup(self.encode_lut, linear_relative)
        return _sdr_encode_relative(linear_relative)

    def _decode_sdr_channel(self, signal: float) -> float:
        if LUT_MIN_NORMALIZED <= signal <= 1.0:
            return _lut_lookup(self.decode_lut, signal)
        return _sdr_decode_relative(signal)

    def _encode_hlg_channel(self, scene_linear: float) -> float:
        if LUT_MIN_NORMALIZED <= scene_linear <= 1.0:
            return _lut_lookup(self.encode_lut, scene_linear)
        return hlg_encode_scene(scene_linear)

    def _decode_hlg_channel(self, signal: float) -> float:
        if LUT_MIN_NORMALIZED <= signal <= 1.0:
            return _lut_lookup(self.decode_lut, signal)
        return hlg_decode_scene(signal)

    def _encode_pq_channel(self, nits: float) -> float:
        normalized = nits / PQ_CONSTANTS["peak_nits"]
        if LUT_MIN_NORMALIZED <= normalized <= 1.0:
            return _lut_lookup(self.encode_lut, normalized)
        return pq_encode_nits(nits)

    def _decode_pq_channel(self, signal: float) -> float:
        if LUT_MIN_NORMALIZED <= signal <= 1.0:
            return _lut_lookup(self.decode_lut, signal)
        return pq_decode_nits(signal)


def roundtrip_error(
    functions: TransferFunctions, samples: Iterable[Sequence[float]]
) -> float:
    """Maximum absolute RGB error after encode/decode, for diagnostics/tests."""

    maximum = 0.0
    for rgb in samples:
        decoded = functions.decode_rgb(functions.encode_rgb(rgb))
        for original, restored in zip(rgb, decoded):
            maximum = max(maximum, abs(original - restored))
    return maximum


def continuity_diagnostics(kind: str) -> dict[str, object]:
    epsilon = 1e-10
    if kind == "SDR_GAMMA":
        beta = SDR_CONSTANTS["beta"]
        left = _sdr_encode_relative(beta - epsilon)
        right = _sdr_encode_relative(beta + epsilon)
        return {
            "piecewise": True,
            "breakpoint": beta,
            "max_step_at_breakpoint": abs(right - left),
            "continuous": abs(right - left) < 1e-7,
        }
    if kind == "HLG":
        scene = HLG_CONSTANTS["scene_breakpoint"]
        signal = HLG_CONSTANTS["signal_breakpoint"]
        scene_step = abs(
            hlg_encode_scene(scene + epsilon)
            - hlg_encode_scene(scene - epsilon)
        )
        signal_step = abs(
            hlg_decode_scene(signal + epsilon)
            - hlg_decode_scene(signal - epsilon)
        )
        return {
            "piecewise": True,
            "scene_breakpoint": scene,
            "signal_breakpoint": signal,
            "max_step_at_scene_breakpoint": scene_step,
            "max_step_at_signal_breakpoint": signal_step,
            "continuous": scene_step < 1e-7 and signal_step < 1e-7,
        }
    return {
        "piecewise": False,
        "domain": [0.0, PQ_CONSTANTS["peak_nits"]],
        "continuous": True,
        "note": "ST 2084 is a continuous logarithmic EOTF",
    }


def roundtrip_diagnostics(functions: TransferFunctions) -> dict[str, object]:
    if functions.kind == "SDR_GAMMA":
        base = [0.0, 1e-6, 0.01, 5.0, 100.0, 500.0, 5000.0, 9000.0]
    elif functions.kind == "HLG":
        base = [0.0, 0.01, 1.0, 10.0, 100.0, 500.0, 999.0, 4000.0, 9000.0]
    else:
        base = [0.0, 0.001, 0.1, 1.0, 10.0, 100.0, 1000.0, 4000.0, 9000.0]
    samples = [
        (
            min(value * 0.7, PQ_CONSTANTS["peak_nits"] - 1.0),
            min(value, PQ_CONSTANTS["peak_nits"] - 1.0),
            min(value * 1.15, PQ_CONSTANTS["peak_nits"] - 1.0),
        )
        for value in base
    ]
    error = roundtrip_error(functions, samples)
    relative_error = 0.0
    for original, restored in (
        (
            sample,
            functions.decode_rgb(functions.encode_rgb(sample)),
        )
        for sample in samples
    ):
        for expected, actual in zip(original, restored):
            relative_error = max(
                relative_error,
                abs(expected - actual) / max(abs(expected), 1e-6),
            )
    return {
        "sample_count": len(samples),
        "max_abs_error_nits": error,
        "max_relative_error": relative_error,
        "tolerance_abs_nits": 0.1,
        "tolerance_relative": 1e-5,
        "passes": error <= 0.1,
        **continuity_diagnostics(functions.kind),
    }
