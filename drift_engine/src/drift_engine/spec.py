"""Baseline specification loading and lookup helpers."""

from __future__ import annotations

import json
import re

from . import values

# dotted segments with optional zero-based array index segments: a.b[0].c
CANONICAL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*|\[\d+])*$")


class SpecError(ValueError):
    pass


class BaselineSpec:
    def __init__(self, payload: dict):
        required = ("merge_version", "whitelist_version", "observation_clock_ms",
                    "hosts", "configs", "host_service", "dependencies")
        missing = [name for name in required if name not in payload]
        if missing:
            raise SpecError(f"spec missing fields: {','.join(missing)}")
        self.merge_version = str(payload["merge_version"])
        self.whitelist_version = str(payload["whitelist_version"])
        clock = payload["observation_clock_ms"]
        if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
            raise SpecError("observation_clock_ms must be a non-negative integer")
        self.observation_clock_ms = clock

        self.hosts = list(payload["hosts"])
        if len(set(self.hosts)) != len(self.hosts):
            raise SpecError("duplicate host identifiers in spec")
        self.host_service = dict(payload["host_service"])
        for host in self.hosts:
            if host not in self.host_service:
                raise SpecError(f"host {host!r} lacks service mapping")

        self.configs: dict[str, dict] = {}
        self.alias_to_canonical: dict[str, str] = {}
        for entry in payload["configs"]:
            key = entry["key"]
            if not CANONICAL_KEY_RE.match(key):
                raise SpecError(f"invalid canonical config key: {key!r}")
            if key in self.configs:
                raise SpecError(f"duplicate config key: {key!r}")
            ctype = entry["type"]
            if ctype not in values.TYPES:
                raise SpecError(f"config {key!r} has unsupported type {ctype!r}")
            self.configs[key] = {
                "type": ctype,
                "upper": entry.get("upper"),
                "enum": list(entry["enum"]) if entry.get("enum") is not None else None,
            }
            alias = entry.get("flat_alias")
            if alias:
                if alias in self.alias_to_canonical:
                    raise SpecError(f"duplicate flat alias: {alias!r}")
                self.alias_to_canonical[alias] = key

        raw_freeze = payload.get("freeze", [])
        self.freeze: dict[str, set[str]] = {host: set() for host in self.hosts}
        if isinstance(raw_freeze, list):
            for key in raw_freeze:
                self._require_key(key)
                for host in self.hosts:
                    self.freeze[host].add(key)
        elif isinstance(raw_freeze, dict):
            for host, keys in raw_freeze.items():
                if host not in self.freeze:
                    raise SpecError(f"freeze targets unknown host {host!r}")
                for key in keys:
                    self._require_key(key)
                    self.freeze[host].add(key)
        else:
            raise SpecError("freeze must be a list or host->keys object")

        self.dependencies: list[tuple[str, str]] = []
        for pair in payload["dependencies"]:
            dependent, dependency = pair["dependent"], pair["depends_on"]
            self._require_key(dependent)
            self._require_key(dependency)
            self.dependencies.append((dependent, dependency))
        # de-duplicated, deterministically ordered adjacency
        self.adjacency: dict[str, set[str]] = {key: set() for key in self.configs}
        for dependent, dependency in self.dependencies:
            self.adjacency[dependent].add(dependency)

    def _require_key(self, key: str) -> None:
        if key not in self.configs:
            raise SpecError(f"unknown config key: {key!r}")

    @staticmethod
    def load_file(path: str) -> "BaselineSpec":
        with open(path, "r", encoding="utf-8") as handle:
            return BaselineSpec(json.load(handle))

    def resolve_flat_key(self, flat_key: str) -> str | None:
        if flat_key in self.configs:
            return flat_key
        return self.alias_to_canonical.get(flat_key)

    def type_of(self, canonical_key: str) -> str:
        return self.configs[canonical_key]["type"]

    def enum_whitelist(self, canonical_key: str) -> list[str] | None:
        return self.configs[canonical_key]["enum"]

    def upper_of(self, canonical_key: str):
        return self.configs[canonical_key]["upper"]

    def is_frozen(self, host: str, canonical_key: str) -> bool:
        return canonical_key in self.freeze.get(host, set())

    def service_of(self, host: str) -> str:
        return self.host_service[host]

    def version_tuple(self) -> tuple[str, str]:
        return self.merge_version, self.whitelist_version
