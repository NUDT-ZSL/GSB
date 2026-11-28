"""Canonical phoneme, viseme, channel and emotion definitions."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Iterable


CANONICAL_CHANNELS: tuple[str, ...] = (
    "jaw_open",
    "lip_width",
    "mouth_corner",
    "brow_raise",
    "brow_inner_up",
)

LIP_CHANNELS: tuple[str, ...] = ("jaw_open", "lip_width", "mouth_corner")
EXPRESSION_CHANNELS: tuple[str, ...] = ("brow_raise", "brow_inner_up", "mouth_corner")

CHANNEL_LABELS: dict[str, str] = {
    "jaw_open": "下颌开合",
    "lip_width": "唇形宽度",
    "mouth_corner": "唇角",
    "brow_raise": "眉部上抬",
    "brow_inner_up": "眉内侧上抬",
}

CHANNEL_ALIASES: dict[str, tuple[str, ...]] = {
    "jaw_open": (
        "jaw_open",
        "jawopen",
        "jaw",
        "jawopening",
        "ctrl_jaw",
        "ctrl_jaw_open",
        "Jaw_Open",
    ),
    "lip_width": (
        "lip_width",
        "lipwidth",
        "mouth_width",
        "mouthwidth",
        "lip_wide",
        "ctrl_lip_w",
        "ctrl_lip_width",
        "Lip_Width",
    ),
    "mouth_corner": (
        "mouth_corner",
        "mouthcorner",
        "corner",
        "mouth_corner_l",
        "mouth_corner_r",
        "mouthcornerl",
        "mouthcornerr",
        "ctrl_mouth_l",
        "ctrl_mouth_corner",
        "Mouth_Corner",
    ),
    "brow_raise": (
        "brow_raise",
        "browraise",
        "brow",
        "brow_l",
        "brow_r",
        "ctrl_brow",
        "Brow_Raise",
    ),
    "brow_inner_up": (
        "brow_inner_up",
        "browinnerup",
        "brow_inner",
        "inner_brow",
        "ctrl_brow_in",
        "ctrl_brow_inner_up",
        "Brow_Inner_Up",
    ),
}


def _alias_key(value: str) -> str:
    return (
        unicodedata.normalize("NFKC", value)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


CHANNEL_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in CHANNEL_ALIASES.items():
    for _alias in _aliases:
        CHANNEL_ALIAS_LOOKUP[_alias_key(_alias)] = _canonical
        CHANNEL_ALIAS_LOOKUP[_alias_key(_alias).replace("_", "")] = _canonical
    CHANNEL_ALIAS_LOOKUP[_alias_key(_canonical)] = _canonical
    CHANNEL_ALIAS_LOOKUP[_alias_key(_canonical).replace("_", "")] = _canonical


def normalize_channel_name(name: str) -> str | None:
    key = _alias_key(name)
    return CHANNEL_ALIAS_LOOKUP.get(key) or CHANNEL_ALIAS_LOOKUP.get(
        key.replace("_", "")
    )


VISEME_TARGETS: dict[str, dict[str, float]] = {
    "SIL": {"jaw_open": 0.00, "lip_width": 0.12, "mouth_corner": 0.00},
    "PP": {"jaw_open": 0.00, "lip_width": 0.08, "mouth_corner": 0.00},
    "FF": {"jaw_open": 0.08, "lip_width": 0.18, "mouth_corner": 0.02},
    "TH": {"jaw_open": 0.16, "lip_width": 0.30, "mouth_corner": 0.02},
    "DD": {"jaw_open": 0.14, "lip_width": 0.36, "mouth_corner": 0.04},
    "SS": {"jaw_open": 0.10, "lip_width": 0.48, "mouth_corner": 0.10},
    "CH": {"jaw_open": 0.20, "lip_width": 0.40, "mouth_corner": 0.08},
    "KK": {"jaw_open": 0.22, "lip_width": 0.33, "mouth_corner": 0.05},
    "HH": {"jaw_open": 0.18, "lip_width": 0.30, "mouth_corner": 0.02},
    "RR": {"jaw_open": 0.20, "lip_width": 0.30, "mouth_corner": 0.02},
    "AA": {"jaw_open": 0.72, "lip_width": 0.64, "mouth_corner": 0.02},
    "E": {"jaw_open": 0.38, "lip_width": 0.78, "mouth_corner": 0.14},
    "I": {"jaw_open": 0.22, "lip_width": 0.72, "mouth_corner": 0.10},
    "O": {"jaw_open": 0.52, "lip_width": 0.36, "mouth_corner": -0.06},
    "U": {"jaw_open": 0.18, "lip_width": 0.22, "mouth_corner": -0.12},
}

CLOSED_VISEMES = frozenset(("SIL", "PP"))


@dataclass(frozen=True)
class PhonemeSpec:
    canonical: str
    category: str
    viseme: str
    arpabet: str
    ipa: str
    legacy: str


# Multiple source labels may resolve to the same visible viseme. The canonical
# token is only an internal identity, never a claim of a one-to-one phoneme map.
_PHONEME_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    ("SIL", "silence", "SIL", "SIL", "#"),
    ("B", "bilabial_plosive", "PP", "B", "b"),
    ("P", "bilabial_plosive", "PP", "P", "p"),
    ("M", "bilabial_nasal", "PP", "M", "m"),
    ("F", "labiodental_fricative", "FF", "F", "f"),
    ("V", "labiodental_fricative", "FF", "V", "v"),
    ("TH", "dental_fricative", "TH", "TH", "θ"),
    ("DH", "dental_fricative", "TH", "DH", "ð"),
    ("T", "alveolar_plosive", "DD", "T", "t"),
    ("D", "alveolar_plosive", "DD", "D", "d"),
    ("N", "alveolar_nasal", "DD", "N", "n"),
    ("L", "alveolar_liquid", "DD", "L", "l"),
    ("S", "alveolar_fricative", "SS", "S", "s"),
    ("Z", "alveolar_fricative", "SS", "Z", "z"),
    ("SH", "postalveolar_fricative", "CH", "SH", "ʃ"),
    ("ZH", "postalveolar_fricative", "CH", "ZH", "ʒ"),
    ("CH", "postalveolar_affricate", "CH", "CH", "tʃ"),
    ("JH", "postalveolar_affricate", "CH", "JH", "dʒ"),
    ("K", "velar_plosive", "KK", "K", "k"),
    ("G", "velar_plosive", "KK", "G", "ɡ"),
    ("NG", "velar_nasal", "KK", "NG", "ŋ"),
    ("HH", "glottal_fricative", "HH", "HH", "h"),
    ("R", "rhotic_liquid", "RR", "R", "r"),
    ("AA", "open_vowel", "AA", "AA", "ɑː"),
    ("AE", "front_open_vowel", "AA", "AE", "æ"),
    ("AH", "central_mid_vowel", "AA", "AH", "ʌ"),
    ("AO", "back_open_vowel", "AA", "AO", "ɔː"),
    ("AW", "diphthong", "AA", "AW", "aʊ"),
    ("AY", "diphthong", "AA", "AY", "aɪ"),
    ("EH", "front_mid_vowel", "E", "EH", "e"),
    ("EY", "diphthong", "E", "EY", "eɪ"),
    ("IH", "front_near_high_vowel", "I", "IH", "ɪ"),
    ("IY", "front_high_vowel", "I", "IY", "iː"),
    ("OW", "back_mid_vowel", "O", "OW", "oʊ"),
    ("OY", "diphthong", "O", "OY", "ɔɪ"),
    ("UH", "back_near_high_vowel", "U", "UH", "ʊ"),
    ("UW", "back_high_vowel", "U", "UW", "uː"),
    ("ER", "rhotic_vowel", "RR", "ER", "ɜː"),
    ("W", "labiovelar_glide", "U", "W", "w"),
    ("Y", "palatal_glide", "I", "Y", "j"),
)


def _legacy_label(canonical: str) -> str:
    return f"PH_{canonical}"


PHONEME_SPECS: dict[str, PhonemeSpec] = {}
for _row in _PHONEME_ROWS:
    _canonical, _category, _viseme, _arpabet, _ipa = _row
    PHONEME_SPECS[_canonical] = PhonemeSpec(
        canonical=_canonical,
        category=_category,
        viseme=_viseme,
        arpabet=_arpabet,
        ipa=_ipa,
        legacy=_legacy_label(_canonical),
    )


def _build_alias_map(version_style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for canonical, spec in PHONEME_SPECS.items():
        labels: Iterable[str]
        if version_style == "ipa":
            labels = (spec.ipa, canonical)
        elif version_style == "legacy":
            labels = (spec.legacy, canonical)
        else:
            labels = (spec.arpabet, canonical)
        for label in labels:
            result[_alias_key(label)] = canonical
    return result


PHONEME_ALIASES: dict[str, dict[str, str]] = {
    "a": _build_alias_map("arpabet"),
    "b": _build_alias_map("ipa"),
    "c": _build_alias_map("legacy"),
}
# Named styles make hand-authored fixtures less dependent on the generated A/B/C
# directory names.
PHONEME_ALIASES.update(
    {
        "arpabet": PHONEME_ALIASES["a"],
        "ipa": PHONEME_ALIASES["b"],
        "legacy": PHONEME_ALIASES["c"],
        "sampa": PHONEME_ALIASES["b"],
        "canonical": {_alias_key(k): k for k in PHONEME_SPECS},
    }
)


def normalize_phoneme_label(label: str, style: str | None = None) -> str | None:
    key = _alias_key(label)
    if style:
        style_key = style.strip().lower()
        mapping = PHONEME_ALIASES.get(style_key)
        if mapping and key in mapping:
            return mapping[key]
    for mapping in PHONEME_ALIASES.values():
        if key in mapping:
            return mapping[key]
    return None


def source_label(canonical: str, style: str) -> str:
    spec = PHONEME_SPECS[canonical]
    if style in ("ipa", "b"):
        return spec.ipa
    if style in ("legacy", "c"):
        return spec.legacy
    return spec.arpabet


def viseme_targets(viseme: str) -> dict[str, float]:
    return dict(VISEME_TARGETS[viseme])


def phoneme_reference(canonical: str) -> dict[str, object]:
    spec = PHONEME_SPECS[canonical]
    return {
        "canonical": canonical,
        "category": spec.category,
        "viseme": spec.viseme,
        "targets": viseme_targets(spec.viseme),
    }


EMOTION_PROFILES: dict[str, dict[str, float]] = {
    "neutral": {"brow_raise": 0.00, "brow_inner_up": 0.00, "mouth_corner": 0.00},
    "joy": {"brow_raise": 0.16, "brow_inner_up": 0.04, "mouth_corner": 0.15},
    "sad": {"brow_raise": -0.05, "brow_inner_up": 0.16, "mouth_corner": -0.12},
    "sadness": {"brow_raise": -0.05, "brow_inner_up": 0.16, "mouth_corner": -0.12},
    "anger": {"brow_raise": -0.10, "brow_inner_up": 0.08, "mouth_corner": -0.08},
    "surprise": {"brow_raise": 0.22, "brow_inner_up": 0.10, "mouth_corner": 0.04},
    "concern": {"brow_raise": 0.02, "brow_inner_up": 0.14, "mouth_corner": -0.05},
}

EMOTION_ALIASES: dict[str, str] = {
    "neutral": "neutral",
    "calm": "neutral",
    "joy": "joy",
    "happy": "joy",
    "happiness": "joy",
    "sad": "sad",
    "sadness": "sadness",
    "anger": "anger",
    "angry": "anger",
    "surprise": "surprise",
    "surprised": "surprise",
    "concern": "concern",
    "worried": "concern",
}

CONFLICT_POLICY = "stress_over_emotion"


def normalize_emotion(label: str | None) -> str:
    if not label:
        return "neutral"
    key = _alias_key(label)
    return EMOTION_ALIASES.get(key, "neutral")


def emotion_profile(label: str | None) -> dict[str, float]:
    return dict(EMOTION_PROFILES[normalize_emotion(label)])


def smoothstep(value: float) -> float:
    x = min(1.0, max(0.0, value))
    return x * x * (3.0 - 2.0 * x)


def raised_cosine(value: float) -> float:
    x = min(1.0, max(0.0, value))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def raised_cosine_window(distance_seconds: float, half_width_seconds: float) -> float:
    if half_width_seconds <= 0.0:
        return 0.0
    if abs(distance_seconds) >= half_width_seconds:
        return 0.0
    phase = abs(distance_seconds) / half_width_seconds
    return 0.5 + 0.5 * math.cos(math.pi * phase)
