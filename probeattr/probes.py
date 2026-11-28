"""Probe record loading with strict per-record validation."""

import json

from .errors import BatchError
from .model import FAILURE_MODES

REQUIRED_FIELDS = ("tick", "service", "interface", "node", "status",
                   "latency_ms", "failure_reason")


def load_probes(path, catalog):
    records = []
    last_tick = {}
    try:
        fh = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        raise BatchError("E_INPUT_NOT_FOUND", f"probes not found: {path}", path=path)
    with fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BatchError("E_INVALID_RECORD", f"line {line_no} is not valid JSON: {exc}",
                                 line=line_no)
            if not isinstance(rec, dict):
                raise BatchError("E_INVALID_RECORD", f"line {line_no} must be a JSON object",
                                 line=line_no)
            for field in REQUIRED_FIELDS:
                if field not in rec:
                    raise BatchError("E_MISSING_FIELD",
                                     f"line {line_no} missing required field '{field}'",
                                     line=line_no, field=field)
            tick = rec["tick"]
            status = rec["status"]
            latency = rec["latency_ms"]
            reason = rec["failure_reason"]
            if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
                raise BatchError("E_INVALID_RECORD", "tick must be a non-negative integer",
                                 line=line_no, value=tick)
            if not isinstance(status, int) or isinstance(status, bool) or status < 0:
                raise BatchError("E_INVALID_RECORD", "status must be a non-negative integer",
                                 line=line_no, value=status)
            if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0:
                raise BatchError("E_INVALID_RECORD", "latency_ms must be a non-negative number",
                                 line=line_no, value=latency)
            if reason not in FAILURE_MODES:
                raise BatchError("E_INVALID_RECORD",
                                 f"failure_reason must be one of {sorted(FAILURE_MODES)}",
                                 line=line_no, value=reason)
            if (status == 200) != (reason == "none"):
                raise BatchError("E_INVALID_RECORD",
                                 "status 200 must pair with failure_reason 'none' and vice versa",
                                 line=line_no, status=status, failure_reason=reason)
            service = rec["service"]
            interface = rec["interface"]
            if service not in catalog.services:
                raise BatchError("E_UNKNOWN_SERVICE",
                                 f"service '{service}' is not registered in the catalog",
                                 line=line_no, service=service)
            if interface not in catalog.services[service]["interfaces"]:
                raise BatchError("E_UNKNOWN_INTERFACE",
                                 f"interface '{interface}' is not registered for service "
                                 f"'{service}' in the catalog",
                                 line=line_no, service=service, interface=interface)
            key = (service, interface)
            prev = last_tick.get(key)
            if prev is not None and tick < prev:
                raise BatchError("E_TS_OUT_OF_ORDER",
                                 f"timestamps out of order for {service}/{interface}: "
                                 f"tick {tick} follows tick {prev}",
                                 line=line_no, service=service, interface=interface,
                                 tick=tick, previous_tick=prev)
            last_tick[key] = tick
            records.append({
                "tick": tick,
                "service": service,
                "interface": interface,
                "node": str(rec["node"]),
                "status": status,
                "latency_ms": float(latency),
                "failure_reason": reason,
            })
    if not records:
        raise BatchError("E_INVALID_RECORD", "probe file contains no records", path=path)
    return records
