"""Deterministic report assembly and persistence."""

import hashlib
import json
import os

from . import __version__


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_fingerprint(strategy):
    return hashlib.sha256(canonical_json(strategy).encode("utf-8")).hexdigest()


def build_report(strategy, events, sporadic, team_summary, input_paths):
    return {
        "report_kind": "availability-attribution",
        "generator": f"probeattr/{__version__}",
        "parameter_version": strategy["parameter_version"],
        "config_fingerprint": config_fingerprint(strategy),
        "input_digest": {
            name: sha256_file(path) for name, path in sorted(input_paths.items())
        },
        "strategy_summary": {
            "probe_period_seconds": strategy["probe_period_seconds"],
            "window_length": strategy["window_length"],
            "consecutive_failure_threshold": strategy["consecutive_failure_threshold"],
            "latency_consecutive_threshold": strategy["latency_consecutive_threshold"],
            "failure_rate_threshold": strategy["failure_rate_threshold"],
            "latency_percentile": strategy["latency_percentile"],
            "latency_threshold_ms": strategy["latency_threshold_ms"],
            "scoring_weights": strategy["scoring_weights"],
        },
        "event_count": len(events),
        "events": events,
        "sporadic_failures": sporadic,
        "team_summary": team_summary,
    }


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(canonical_json(obj))
    os.replace(tmp, path)


def write_aggregates_atomic(path, aggregates):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for key in sorted(aggregates):
            for row in aggregates[key]:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                fh.write("\n")
    os.replace(tmp, path)
