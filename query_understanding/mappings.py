"""Traditional/Simplified and full-width/half-width normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import E_MAP_MISSING, QUError
from .util import payload_checksum


def _is_fullwidth(char: str) -> bool:
    code = ord(char)
    return code == 0x3000 or 0xFF01 <= code <= 0xFF5E


@dataclass(frozen=True)
class MappingTable:
    version: int
    fullwidth_to_halfwidth: dict[str, str]
    traditional_to_simplified: dict[str, str]
    traditional_sources: frozenset[str]
    checksum: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MappingTable":
        required = (
            "version",
            "fullwidth_to_halfwidth",
            "traditional_to_simplified",
            "traditional_sources",
        )
        for key in required:
            if key not in payload:
                raise QUError(
                    E_MAP_MISSING,
                    f"mapping payload is missing required field {key}",
                    stage="normalize",
                    details={"missing_field": key},
                )
        version = int(payload["version"])
        if version < 1:
            raise QUError(
                E_MAP_MISSING,
                "mapping version must be a positive integer",
                stage="normalize",
                details={"version": version},
            )
        fw = payload["fullwidth_to_halfwidth"]
        ts = payload["traditional_to_simplified"]
        sources = payload["traditional_sources"]
        if not isinstance(fw, dict) or not isinstance(ts, dict) or not isinstance(sources, list):
            raise QUError(
                E_MAP_MISSING,
                "mapping tables have invalid types",
                stage="normalize",
            )
        for source, target in list(fw.items()) + list(ts.items()):
            if not isinstance(source, str) or len(source) != 1:
                raise QUError(
                    E_MAP_MISSING,
                    "mapping source must be exactly one code point",
                    stage="normalize",
                    details={"source": repr(source)},
                )
            if not isinstance(target, str) or not target:
                raise QUError(
                    E_MAP_MISSING,
                    "mapping target is missing",
                    stage="normalize",
                    details={"source": source},
                )
        for source in sources:
            if source not in ts:
                raise QUError(
                    E_MAP_MISSING,
                    "declared traditional source has no mapping",
                    stage="normalize",
                    details={"source": source},
                )
        expected = payload.get("checksum")
        actual = payload_checksum(payload)
        if expected is not None and expected != actual:
            raise QUError(
                E_MAP_MISSING,
                "mapping checksum mismatch",
                stage="normalize",
                details={"expected": expected, "actual": actual},
            )
        return cls(
            version=version,
            fullwidth_to_halfwidth={str(k): str(v) for k, v in fw.items()},
            traditional_to_simplified={str(k): str(v) for k, v in ts.items()},
            traditional_sources=frozenset(str(value) for value in sources),
            checksum=actual,
        )

    def normalize(self, text: str) -> str:
        if not isinstance(text, str):
            raise QUError(
                E_MAP_MISSING,
                "query_text must be a string",
                stage="normalize",
                details={"actual_type": type(text).__name__},
            )
        chars: list[str] = []
        for char in text:
            mapped = char
            if _is_fullwidth(char):
                if char not in self.fullwidth_to_halfwidth:
                    raise QUError(
                        E_MAP_MISSING,
                        "full-width code point is absent from the mapping table",
                        stage="normalize",
                        category="fullwidth",
                        details={"char": char, "codepoint": f"U+{ord(char):04X}"},
                    )
                mapped = self.fullwidth_to_halfwidth[char]
            elif char in self.traditional_sources:
                if char not in self.traditional_to_simplified:
                    raise QUError(
                        E_MAP_MISSING,
                        "traditional code point is absent from the mapping table",
                        stage="normalize",
                        category="traditional",
                        details={"char": char, "codepoint": f"U+{ord(char):04X}"},
                    )
                mapped = self.traditional_to_simplified[char]
            chars.append(mapped)

        lowered = "".join(chars).lower()
        collapsed = " ".join(lowered.split())
        return collapsed

