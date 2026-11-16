"""Versioned config manifest loading and strict validation (E_MANIFEST)."""

import json
import re

from .errors import E_MANIFEST, PlatformError

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
STRATEGY_MODES = ("full", "shard")


class Manifest:
    def __init__(self, version, items, targets, strategy):
        self.version = version
        self.items = items
        self.targets = targets
        self.strategy = strategy

    def to_dict(self):
        return {
            "version": self.version,
            "items": self.items,
            "targets": self.targets,
            "strategy": self.strategy,
        }

    @staticmethod
    def from_dict(body):
        return Manifest(body["version"], body["items"], body["targets"], body["strategy"])


def _reject(location, message):
    raise PlatformError(
        E_MANIFEST,
        "manifest rejected at %s: %s" % (location, message),
        details={"location": location},
    )


def validate_manifest(body, registry_ids, min_version=0):
    """Validate a raw manifest dict; returns a Manifest. Rejects as a whole."""
    if not isinstance(body, dict):
        _reject("$", "manifest root must be a JSON object")

    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _reject("$.version", "version must be a positive integer")
    if version <= min_version:
        _reject(
            "$.version",
            "version %d must be strictly greater than committed version %d"
            % (version, min_version),
        )

    items = body.get("items")
    if not isinstance(items, dict) or not items:
        _reject("$.items", "items must be a non-empty object of key/value pairs")
    for key in items:
        if not KEY_RE.match(key):
            _reject("$.items.%s" % key, "illegal config key (pattern %s)" % KEY_RE.pattern)
        if not isinstance(items[key], (str, int, float, bool)):
            _reject("$.items.%s" % key, "config value must be a scalar")

    targets = body.get("targets")
    if not isinstance(targets, list) or not targets:
        _reject("$.targets", "targets must be a non-empty array of node ids")
    seen = set()
    for idx, node in enumerate(targets):
        if not isinstance(node, str):
            _reject("$.targets[%d]" % idx, "target node id must be a string")
        if node in seen:
            _reject("$.targets[%d]" % idx, "duplicate target node %s" % node)
        seen.add(node)
        if node not in registry_ids:
            _reject(
                "$.targets[%d]" % idx,
                "target node %s is not present in the node registry" % node,
            )

    strategy = body.get("strategy")
    if not isinstance(strategy, dict):
        _reject("$.strategy", "strategy must be an object")
    mode = strategy.get("mode")
    if mode not in STRATEGY_MODES:
        _reject("$.strategy.mode", "mode must be one of %s" % (STRATEGY_MODES,))
    if mode == "shard":
        shards = strategy.get("shards")
        if not isinstance(shards, int) or isinstance(shards, bool) or shards < 1:
            _reject("$.strategy.shards", "shard strategy requires integer shards >= 1")

    return Manifest(version, dict(items), list(targets), dict(strategy))


def load_manifest(path, registry_ids, min_version=0):
    """Load and strictly validate a manifest JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except FileNotFoundError:
        _reject("$", "manifest file not found: %s" % path)
    except json.JSONDecodeError as exc:
        _reject("$", "invalid JSON: %s" % exc)
    return validate_manifest(body, registry_ids, min_version=min_version)
