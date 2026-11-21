"""Hierarchical domain/intent/slot taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_TAXONOMY_INVALID, QUError
from .util import payload_checksum, read_json


@dataclass(frozen=True)
class SlotSpec:
    name: str
    value_type: str
    required: bool
    regex: str | None
    normalize: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class IntentSpec:
    domain: str
    name: str
    slot_schema: tuple[SlotSpec, ...]

    @property
    def slot_names(self) -> tuple[str, ...]:
        return tuple(slot.name for slot in self.slot_schema)


@dataclass
class Taxonomy:
    version: int
    checksum: str
    domains: tuple[str, ...]
    intents: dict[tuple[str, str], IntentSpec]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Taxonomy":
        if "version" not in payload or "domains" not in payload:
            raise QUError(
                E_TAXONOMY_INVALID,
                "taxonomy payload must contain version and domains",
                stage="intent",
            )
        version = int(payload["version"])
        domains_payload = payload["domains"]
        if not isinstance(domains_payload, list) or not domains_payload:
            raise QUError(
                E_TAXONOMY_INVALID,
                "taxonomy domains must be a non-empty list",
                stage="intent",
            )
        domains: list[str] = []
        intents: dict[tuple[str, str], IntentSpec] = {}
        for domain_row in domains_payload:
            domain = str(domain_row.get("name", ""))
            if not domain or domain in domains:
                raise QUError(
                    E_TAXONOMY_INVALID,
                    "domain names must be unique and non-empty",
                    stage="intent",
                    details={"domain": domain},
                )
            domains.append(domain)
            intent_rows = domain_row.get("intent_classes")
            if not isinstance(intent_rows, list) or not intent_rows:
                raise QUError(
                    E_TAXONOMY_INVALID,
                    "each domain must contain intent_classes",
                    stage="intent",
                    details={"domain": domain},
                )
            for intent_row in intent_rows:
                name = str(intent_row.get("name", ""))
                key = (domain, name)
                if not name or key in intents:
                    raise QUError(
                        E_TAXONOMY_INVALID,
                        "intent classes must be unique within a domain",
                        stage="intent",
                        details={"domain": domain, "intent_class": name},
                    )
                raw_slots = intent_row.get("slot_schema", [])
                if not isinstance(raw_slots, list):
                    raise QUError(
                        E_TAXONOMY_INVALID,
                        "slot_schema must be a list",
                        stage="intent",
                        details={"domain": domain, "intent_class": name},
                    )
                slots: list[SlotSpec] = []
                seen_slots: set[str] = set()
                for slot_row in raw_slots:
                    slot_name = str(slot_row.get("name", ""))
                    if not slot_name or slot_name in seen_slots:
                        raise QUError(
                            E_TAXONOMY_INVALID,
                            "slot names must be unique and non-empty",
                            stage="intent",
                            details={"domain": domain, "intent_class": name, "slot": slot_name},
                        )
                    seen_slots.add(slot_name)
                    regex = slot_row.get("regex")
                    if regex is not None and not isinstance(regex, str):
                        raise QUError(
                            E_TAXONOMY_INVALID,
                            "slot regex must be a string when present",
                            stage="intent",
                        )
                    slots.append(
                        SlotSpec(
                            name=slot_name,
                            value_type=str(slot_row.get("value_type", "string")),
                            required=bool(slot_row.get("required", False)),
                            regex=regex,
                            normalize=str(slot_row.get("normalize", "identity")),
                            aliases=tuple(str(value) for value in slot_row.get("aliases", [])),
                        )
                    )
                intents[key] = IntentSpec(
                    domain=domain,
                    name=name,
                    slot_schema=tuple(slots),
                )
        expected = payload.get("checksum")
        actual = payload_checksum(payload)
        if expected is not None and expected != actual:
            raise QUError(
                E_TAXONOMY_INVALID,
                "taxonomy checksum mismatch",
                stage="intent",
                details={"expected": expected, "actual": actual},
            )
        return cls(
            version=version,
            checksum=actual,
            domains=tuple(domains),
            intents=intents,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Taxonomy":
        return cls.from_payload(read_json(path))

    def intent(self, domain: str, intent_class: str) -> IntentSpec | None:
        return self.intents.get((domain, intent_class))

    def require_intent(self, domain: str, intent_class: str) -> IntentSpec:
        intent = self.intent(domain, intent_class)
        if intent is None:
            raise QUError(
                E_TAXONOMY_INVALID,
                "intent is absent from taxonomy",
                stage="intent",
                category="intent_taxonomy_mismatch",
                details={"domain": domain, "intent_class": intent_class},
            )
        return intent

    def sorted_intents(self) -> list[tuple[str, str]]:
        return sorted(self.intents)

