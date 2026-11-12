"""Report serialisation, legacy-report merging and integrity verification."""

import json
import os

from . import SCHEMA_VERSION
from .analyzer import (build_aggregate, build_groups, sha256_file)
from .rules import (BUILTIN_RULESET, BUILTIN_RULESET_ID, load_ruleset,
                    ruleset_fingerprint)

DOCUMENT_DEFAULTS = {
    "id": "unknown",
    "path": "",
    "format": "unknown",
    "status": "unknown",
    "input_sha256": "",
    "size_bytes": 0,
    "analysis_ms": 0.0,
    "truncated": False,
    "modules": [],
    "features": [],
    "matched_rules": [],
    "confidence": 0.0,
    "errors": [],
    "duplicate_of": None,
    "payload_fingerprint": None,
}

FEATURE_DEFAULTS = {
    "type": "unknown",
    "evidence": {"module": "", "statement": 0, "snippet": ""},
}


def build_report(records, ruleset, rules_path, rules_load_error,
                 concurrency_peak, max_workers, max_bytes, timeout_ms,
                 warnings=None):
    groups = build_groups(records)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "macroanalyzer",
        "used_ruleset": ruleset["ruleset_id"],
        "used_ruleset_fingerprint": ruleset_fingerprint(ruleset),
        "rules_path": rules_path,
        "rules_load_error": rules_load_error,
        "warnings": warnings or [],
        "concurrency": {"max_workers": max_workers, "peak": concurrency_peak},
        "limits": {"max_bytes": max_bytes, "timeout_ms": timeout_ms},
        "documents": records,
        "groups": groups,
        "aggregate": build_aggregate(records, groups),
    }


def write_report(report, out_path):
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_report(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------- merge

def _fill_defaults(doc, doc_label, warnings):
    filled = dict(doc)
    for key, default in DOCUMENT_DEFAULTS.items():
        if key not in filled:
            filled[key] = default
            warnings.append("%s: missing field %r -> default %r"
                            % (doc_label, key, default))
    features = []
    for j, feat in enumerate(filled.get("features") or []):
        feat = dict(feat)
        for key, default in FEATURE_DEFAULTS.items():
            if key not in feat:
                feat[key] = default
                warnings.append("%s.features[%d]: missing field %r -> default"
                                % (doc_label, j, key))
        features.append(feat)
    filled["features"] = features
    return filled


def merge_reports(paths):
    """Merge old-schema reports into a current-schema report.

    Missing fields are filled with defaults and listed in warnings.
    Never raises on missing fields; returns (report, warnings).
    """
    warnings = []
    documents = []
    for path in paths:
        try:
            old = load_report(path)
        except Exception as exc:
            warnings.append("%s: unreadable report (%s); skipped" % (path, exc))
            continue
        if "schema_version" not in old:
            warnings.append("%s: missing field 'schema_version' -> default %r"
                            % (path, SCHEMA_VERSION))
        old_docs = old.get("documents")
        if not isinstance(old_docs, list):
            warnings.append("%s: missing field 'documents' -> default []"
                            % path)
            old_docs = []
        for i, doc in enumerate(old_docs):
            if not isinstance(doc, dict):
                warnings.append("%s.documents[%d]: not an object; skipped"
                                % (path, i))
                continue
            documents.append(_fill_defaults(
                doc, "%s.documents[%d]" % (path, i), warnings))
    groups = build_groups(documents)
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "macroanalyzer",
        "used_ruleset": "merged",
        "used_ruleset_fingerprint": None,
        "rules_path": None,
        "rules_load_error": None,
        "warnings": warnings,
        "concurrency": {"max_workers": 1, "peak": 0},
        "limits": {"max_bytes": None, "timeout_ms": None},
        "documents": documents,
        "groups": groups,
        "aggregate": build_aggregate(documents, groups),
    }
    return report, warnings


# ------------------------------------------------------------------ verify

def verify_report(report_path, rules_path=None):
    """Recompute input hashes and ruleset fingerprint; compare with report.

    Returns (ok, mismatches) where each mismatch is a human-readable string.
    """
    report = load_report(report_path)
    mismatches = []
    if not isinstance(report.get("schema_version"), str):
        mismatches.append("report: schema_version missing or not a string")
    for doc in report.get("documents", []):
        doc_id = doc.get("id", "?")
        path = doc.get("path", "")
        expected = doc.get("input_sha256")
        if not path or not os.path.isfile(path):
            mismatches.append("%s: input file missing: %s" % (doc_id, path))
            continue
        actual = sha256_file(path)
        if actual != expected:
            mismatches.append(
                "%s: input_sha256 mismatch (report=%s actual=%s)"
                % (doc_id, expected, actual))
    expected_fp = report.get("used_ruleset_fingerprint")
    if expected_fp is not None:
        if report.get("used_ruleset") == BUILTIN_RULESET_ID:
            actual_fp = ruleset_fingerprint(BUILTIN_RULESET)
        else:
            src = rules_path or report.get("rules_path")
            if not src or not os.path.isfile(src):
                actual_fp = None
                mismatches.append(
                    "report: cannot recompute ruleset fingerprint; "
                    "rules file not found: %r" % src)
            else:
                ruleset, err = load_ruleset(src)
                if err:
                    mismatches.append("report: rules file failed to load: %s"
                                      % err)
                    actual_fp = None
                else:
                    actual_fp = ruleset_fingerprint(ruleset)
        if actual_fp is not None and actual_fp != expected_fp:
            mismatches.append(
                "report: used_ruleset_fingerprint mismatch "
                "(report=%s actual=%s)" % (expected_fp, actual_fp))
    return (not mismatches), mismatches
