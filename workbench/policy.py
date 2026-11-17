"""Unified policy intermediate representation and v1/v2 parsers.

v1 exchange format: single flat ``key=value`` text line, e.g.::

    decoder=sampling temp_milli=800 top_k=40 top_p_milli=950 rp_milli=1100 seed32=12345

v2 exchange format: JSON document, e.g.::

    {"decoder": {"type": "beam", "beam_width": 4, "length_alpha": 0.7},
     "temperature": 0.8, "top_k": 40, "top_p": 0.95,
     "repetition_penalty": 1.1, "seed": 123456789012}
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .errors import (
    E_POLICY_RANGE,
    E_POLICY_TYPE,
    E_POLICY_UNKNOWN_KEY,
    WorkbenchError,
)

UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1

DECODERS_V1 = ("greedy", "sampling")
DECODERS_V2 = ("greedy", "sampling", "beam")

# (min, max) inclusive ranges for the unified IR fields.
RANGES = {
    "temperature": (0.0, 10.0),
    "top_k": (0, 100000),
    "top_p": (0.0, 1.0),
    "repetition_penalty": (0.0, 5.0),
    "beam_width": (1, 64),
    "length_alpha": (-5.0, 5.0),
}

V1_KEYS = ("decoder", "temp_milli", "top_k", "top_p_milli", "rp_milli", "seed32")
V2_KEYS = ("decoder", "temperature", "top_k", "top_p", "repetition_penalty", "seed")
V2_DECODER_KEYS = ("type", "beam_width", "length_alpha")

_INT_RE = re.compile(r"^[+-]?\d+$")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value):
    return _is_int(value) or isinstance(value, float)


@dataclass
class Policy:
    """Unified intermediate representation of a decoding policy."""

    decoder_type: str
    temperature: float
    top_k: int
    top_p: float
    repetition_penalty: float
    seed: int
    beam_width: int = 1
    length_alpha: float = 0.0
    source_version: str = "v2"
    raw_fields: dict = field(default_factory=dict)

    def normalized(self):
        """Canonical dict used for fingerprinting (rounded to kill float noise)."""
        return {
            "decoder_type": self.decoder_type,
            "temperature": round(float(self.temperature), 9),
            "top_k": int(self.top_k),
            "top_p": round(float(self.top_p), 9),
            "repetition_penalty": round(float(self.repetition_penalty), 9),
            "seed": int(self.seed),
            "beam_width": int(self.beam_width),
            "length_alpha": round(float(self.length_alpha), 9),
        }

    def fingerprint(self):
        blob = json.dumps(self.normalized(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def with_fields(self, **changes):
        data = {
            "decoder_type": self.decoder_type,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
            "seed": self.seed,
            "beam_width": self.beam_width,
            "length_alpha": self.length_alpha,
            "source_version": self.source_version,
            "raw_fields": dict(self.raw_fields),
        }
        data.update(changes)
        return Policy(**data)


def _check_range(name, value, lo, hi):
    if not (lo <= value <= hi):
        raise WorkbenchError(
            E_POLICY_RANGE, f"field {name!r} value {value!r} out of range [{lo}, {hi}]"
        )


def _parse_v1_int(name, text):
    if not _INT_RE.match(text):
        raise WorkbenchError(E_POLICY_TYPE, f"field {name!r} expects integer, got {text!r}")
    return int(text)


def parse_v1_line(line):
    """Parse a v1 flat key=value line into a Policy. Raises WorkbenchError."""
    text = line.strip()
    if not text:
        raise WorkbenchError(E_POLICY_TYPE, "empty v1 policy line")
    fields = {}
    for chunk in text.split():
        if "=" not in chunk:
            raise WorkbenchError(E_POLICY_TYPE, f"malformed token {chunk!r} in v1 policy line")
        key, _, value = chunk.partition("=")
        if key not in V1_KEYS:
            raise WorkbenchError(E_POLICY_UNKNOWN_KEY, f"unknown v1 policy key {key!r}")
        fields[key] = value
    missing = [k for k in V1_KEYS if k not in fields]
    if missing:
        raise WorkbenchError(E_POLICY_TYPE, f"v1 policy line missing fields: {missing}")

    decoder = fields["decoder"]
    if decoder not in DECODERS_V1:
        raise WorkbenchError(
            E_POLICY_RANGE,
            f"v1 decoder must be one of {DECODERS_V1}, got {decoder!r}",
        )
    temp_milli = _parse_v1_int("temp_milli", fields["temp_milli"])
    top_k = _parse_v1_int("top_k", fields["top_k"])
    top_p_milli = _parse_v1_int("top_p_milli", fields["top_p_milli"])
    rp_milli = _parse_v1_int("rp_milli", fields["rp_milli"])
    seed32 = _parse_v1_int("seed32", fields["seed32"])

    _check_range("temp_milli", temp_milli, 0, int(RANGES["temperature"][1] * 1000))
    _check_range("top_k", top_k, *RANGES["top_k"])
    _check_range("top_p_milli", top_p_milli, 0, 1000)
    _check_range("rp_milli", rp_milli, 0, int(RANGES["repetition_penalty"][1] * 1000))
    _check_range("seed32", seed32, 0, UINT32_MAX)

    return Policy(
        decoder_type=decoder,
        temperature=temp_milli / 1000.0,
        top_k=top_k,
        top_p=top_p_milli / 1000.0,
        repetition_penalty=rp_milli / 1000.0,
        seed=seed32,
        beam_width=1,
        length_alpha=0.0,
        source_version="v1",
        raw_fields=dict(fields),
    )


def parse_v2_doc(doc):
    """Parse a v2 JSON document (dict) into a Policy. Raises WorkbenchError."""
    if not isinstance(doc, dict):
        raise WorkbenchError(E_POLICY_TYPE, "v2 policy document must be a JSON object")
    for key in doc:
        if key not in V2_KEYS:
            raise WorkbenchError(E_POLICY_UNKNOWN_KEY, f"unknown v2 policy key {key!r}")
    missing = [k for k in V2_KEYS if k not in doc]
    if missing:
        raise WorkbenchError(E_POLICY_TYPE, f"v2 policy document missing fields: {missing}")

    dec = doc["decoder"]
    if not isinstance(dec, dict):
        raise WorkbenchError(E_POLICY_TYPE, "v2 'decoder' must be an object")
    for key in dec:
        if key not in V2_DECODER_KEYS:
            raise WorkbenchError(E_POLICY_UNKNOWN_KEY, f"unknown v2 decoder key {key!r}")
    if "type" not in dec:
        raise WorkbenchError(E_POLICY_TYPE, "v2 decoder missing 'type'")
    dtype = dec["type"]
    if not isinstance(dtype, str):
        raise WorkbenchError(E_POLICY_TYPE, "v2 decoder 'type' must be a string")
    if dtype not in DECODERS_V2:
        raise WorkbenchError(
            E_POLICY_RANGE, f"v2 decoder must be one of {DECODERS_V2}, got {dtype!r}"
        )

    beam_width = dec.get("beam_width", 1)
    length_alpha = dec.get("length_alpha", 0.0)
    if not _is_int(beam_width):
        raise WorkbenchError(E_POLICY_TYPE, "beam_width must be an integer")
    if not _is_num(length_alpha):
        raise WorkbenchError(E_POLICY_TYPE, "length_alpha must be a number")
    if dtype == "beam":
        _check_range("beam_width", beam_width, *RANGES["beam_width"])
        _check_range("length_alpha", float(length_alpha), *RANGES["length_alpha"])

    temperature = doc["temperature"]
    top_k = doc["top_k"]
    top_p = doc["top_p"]
    rp = doc["repetition_penalty"]
    seed = doc["seed"]
    if not _is_num(temperature):
        raise WorkbenchError(E_POLICY_TYPE, "temperature must be a number")
    if not _is_int(top_k):
        raise WorkbenchError(E_POLICY_TYPE, "top_k must be an integer")
    if not _is_num(top_p):
        raise WorkbenchError(E_POLICY_TYPE, "top_p must be a number")
    if not _is_num(rp):
        raise WorkbenchError(E_POLICY_TYPE, "repetition_penalty must be a number")
    if not _is_int(seed):
        raise WorkbenchError(E_POLICY_TYPE, "seed must be an integer")

    _check_range("temperature", float(temperature), *RANGES["temperature"])
    _check_range("top_k", top_k, *RANGES["top_k"])
    _check_range("top_p", float(top_p), *RANGES["top_p"])
    _check_range("repetition_penalty", float(rp), *RANGES["repetition_penalty"])
    _check_range("seed", seed, 0, UINT64_MAX)

    return Policy(
        decoder_type=dtype,
        temperature=float(temperature),
        top_k=top_k,
        top_p=float(top_p),
        repetition_penalty=float(rp),
        seed=seed,
        beam_width=beam_width if dtype == "beam" else 1,
        length_alpha=float(length_alpha) if dtype == "beam" else 0.0,
        source_version="v2",
        raw_fields=json.loads(json.dumps(doc)),
    )


def parse_v2_text(text):
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkbenchError(E_POLICY_TYPE, f"invalid v2 JSON: {exc}") from exc
    return parse_v2_doc(doc)


def parse_policy_text(text, version):
    if version == "v1":
        return parse_v1_line(text)
    if version == "v2":
        return parse_v2_text(text)
    raise WorkbenchError(E_POLICY_TYPE, f"unknown policy version {version!r}")
