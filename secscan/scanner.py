"""Recursive scanning and safe remediation of JSON configuration documents."""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, Union

from .rules import Rule, SEVERITY_LABELS, RulesLoadResult

Segment = Union[str, int]

# Values that reference a secret manager / encrypted envelope are not
# considered "plaintext" by the scanner. This is what makes re-scanning after
# --fix return zero plaintext-secret findings.
_PROTECTED_VALUE_RE = re.compile(
    r"^(?:\$\{[^}]+\}|\$[A-Z_][A-Z0-9_]*|vault://[^\s]+|enc://[^\s]+|"
    r"ENC\([^\n]*\)|<redacted>|__SECSCAN_[A-Z_]+__)$",
    re.IGNORECASE,
)
VAULT_REFERENCE = "${SECSCAN_SECRET_FROM_VAULT}"
MAX_SCAN_DEPTH = 256

_PICKLE_RE = re.compile(r"\b(?:c?pickle|dill|marshal)\.loads?\b", re.IGNORECASE)
_YAML_LOAD_RE = re.compile(r"\byaml\.(?:unsafe_)?load(?:_all)?\b", re.IGNORECASE)
_YAML_SAFE_RE = re.compile(
    r"(?:safe_loader|csafeloader|safeloader|yaml\.safe_load)", re.IGNORECASE
)
_SAFE_TOKEN_RE = re.compile(
    r"\b(?:json\.loads?|yaml\.safe_load|ast\.literal_eval)\b", re.IGNORECASE
)
# A "loader spec" is a bare callable expression such as ``yaml.load`` or
# ``pickle.loads(f)``. Used to scan values under arbitrary key names without
# flagging prose that merely mentions these functions.
_LOADER_SPEC_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^()]*\))?$"
)

# Nested containers and scalar fields that no rule covered are listed so an
# operator can review whether the rule set needs extending.
SKIP_REASON_NESTED = (
    "未知/未匹配规则的嵌套结构，已递归检查但无命中，建议人工复核字段语义 "
    "(unknown nested structure; reviewed recursively, no rule matched)"
)
SKIP_REASON_FIELD = (
    "未知字段，未匹配任何检测规则，已跳过并继续扫描 "
    "(unknown field; no rule matched, scanning continued)"
)
SKIP_REASON_DEPTH = (
    f"嵌套层级超过扫描上限 {MAX_SCAN_DEPTH} 层，已停止深入并保留供人工复核 "
    f"(nesting deeper than MAX_SCAN_DEPTH={MAX_SCAN_DEPTH}; not traversed further)"
)


@dataclass
class Finding:
    """One vulnerability, plus the data needed to remediate it."""

    rule: Rule
    segments: tuple[Segment, ...]
    key: str
    evidence: str

    @property
    def path(self) -> str:
        return format_path(self.segments)

    @property
    def bare_path(self) -> str:
        return format_bare_path(self.segments)

    @property
    def fixable(self) -> bool:
        return self.rule.fixable

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule.rule_id,
            "rule_type": self.rule.rule_type,
            "operator": self.rule.operator,
            "severity": self.rule.severity_label,
            "severity_code": self.rule.severity,
            "path": self.path,
            "bare_path": self.bare_path,
            "key": self.key,
            "description": self.rule.description,
            "evidence": self.evidence,
            "fixable": self.fixable,
        }


@dataclass
class SkippedPath:
    segments: tuple[Segment, ...]
    reason: str
    kind: str  # "nested" | "field"

    @property
    def path(self) -> str:
        return format_path(self.segments)

    @property
    def bare_path(self) -> str:
        return format_bare_path(self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bare_path": self.bare_path,
            "kind": self.kind,
            "reason": self.reason,
        }


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    skipped: list[SkippedPath] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.findings)

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda item: (path_sort_key(item.segments), item.rule.rule_id),
        )

    def sorted_skipped(self) -> list[SkippedPath]:
        return sorted(
            self.skipped, key=lambda item: (path_sort_key(item.segments), item.kind)
        )


@dataclass
class FixOutcome:
    document: Any
    fixes: list[dict[str, Any]]


def _is_identifier(key: str) -> bool:
    # Unicode identifiers are valid dot-notation segments too, so keys such
    # as "配置" stay readable in the report.
    return key.isidentifier()


def format_path(segments: Sequence[Segment]) -> str:
    """Render a JSONPath-like path, e.g. ``$.servers[0].password``."""

    if not segments:
        return "$"
    out = ["$"]
    for segment in segments:
        if isinstance(segment, int):
            out.append(f"[{segment}]")
        elif _is_identifier(segment):
            out.append(f".{segment}")
        else:
            escaped = segment.replace("\\", "\\\\").replace("'", "\\'")
            out.append(f"['{escaped}']")
    return "".join(out)


def format_bare_path(segments: Sequence[Segment]) -> str:
    """Render a human-friendly dotted path without the ``$`` prefix."""

    if not segments:
        return ""
    out = ""
    for segment in segments:
        if isinstance(segment, int):
            out += f"[{segment}]"
        elif _is_identifier(segment):
            out += ("." if out else "") + segment
        else:
            out += f"['{segment}']"
    return out


def path_sort_key(segments: Sequence[Segment]) -> tuple[tuple[int, Any], ...]:
    """Natural path ordering: object keys alphabetically, indices numerically."""

    return tuple(
        (1, segment.lower()) if isinstance(segment, str) else (0, segment)
        for segment in segments
    )


def mask_value(value: Any) -> str:
    """Return a short, non-reversible preview for report evidence."""

    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if len(text) <= 2:
        return "*" * len(text)
    if len(text) <= 8:
        return text[0] + "*" * (len(text) - 2) + text[-1]
    return text[:2] + "*" * 6 + text[-2:]


def is_protected_value(value: Any) -> bool:
    return isinstance(value, str) and bool(_PROTECTED_VALUE_RE.match(value.strip()))


def _password_is_weak(value: Any, weak_passwords: Iterable[str]) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if not isinstance(value, (str, int, float)):
        return False
    normalized = str(value).strip().lower()
    if not normalized:
        return False
    return normalized in set(weak_passwords)


def _dangerous_loader_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _SAFE_TOKEN_RE.search(text):
        return None

    if _PICKLE_RE.search(text):
        return f"危险反序列化设置 {text!r}：pickle/dill/marshal 反序列化可执行任意代码"
    if _YAML_LOAD_RE.search(text):
        if _YAML_SAFE_RE.search(text):
            return None
        return f"危险反序列化设置 {text!r}：yaml.load 系列接口未指定 SafeLoader"
    return None


def _evaluate(rule: Rule, value: Any) -> tuple[bool, str]:
    """Return ``(is_finding, evidence)`` for one field/value pair."""

    if rule.operator == "weak_password":
        if _password_is_weak(value, rule.weak_passwords):
            return True, mask_value(value)
        return False, ""

    if rule.operator == "plaintext_secret":
        if isinstance(value, bool) or value is None:
            return False, ""
        if not isinstance(value, (str, int, float)):
            return False, ""
        if isinstance(value, str) and (not value.strip() or is_protected_value(value)):
            return False, ""
        return True, mask_value(value)

    if rule.operator == "dangerous_loader":
        reason = _dangerous_loader_reason(value)
        if reason:
            return True, reason
        return False, ""

    return False, ""


def scan_document(document: Any, rules: Sequence[Rule]) -> ScanResult:
    """Walk the whole document, recording findings and reviewable paths.

    Unknown nested structures and unknown scalar fields are still traversed
    (so a password buried in an unfamiliar container is not missed), and every
    path that no rule covered is reported under ``skipped`` for manual review.
    Traversal is iterative, so deeply nested input cannot exhaust the Python
    stack; containers deeper than ``MAX_SCAN_DEPTH`` are reported for manual
    review instead of being followed further.
    """

    findings: list[Finding] = []
    # Scalar paths that produced no finding: (path, reason, kind).
    scalar_skips: list[tuple[tuple[Segment, ...], str, str]] = []
    # Container paths and the reason they may be skipped if nothing below hit.
    container_reasons: dict[tuple[Segment, ...], str] = {}
    seen_scalar: set[tuple[Segment, ...]] = set()

    def analyze_scalar(value: Any, segments: tuple[Segment, ...], key: str) -> None:
        hit = False
        key_matched = False
        for rule in rules:
            key_matches = rule.matches_key(key)
            if not key_matches:
                if not rule.match_value_anywhere:
                    continue
                # Under an arbitrary key, only a bare callable expression is
                # a serialization setting; prose or URLs are not.
                if not (
                    isinstance(value, str)
                    and _LOADER_SPEC_RE.match(value.strip())
                ):
                    continue
            else:
                key_matched = True
            matched, evidence = _evaluate(rule, value)
            if matched:
                hit = True
                findings.append(
                    Finding(rule=rule, segments=segments, key=key, evidence=evidence)
                )
        if hit or segments in seen_scalar:
            return
        seen_scalar.add(segments)
        if key_matched:
            # The field name was security-relevant but its value passed the
            # check; keep it visible as reviewed rather than "unknown".
            scalar_skips.append(
                (
                    segments,
                    "字段名匹配规则但取值未命中检测条件，已检查通过 "
                    "(field matched a rule name; value did not trigger a finding)",
                    "field",
                )
            )
        else:
            scalar_skips.append((segments, SKIP_REASON_FIELD, "field"))

    stack: list[tuple[Any, tuple[Segment, ...], str | None, int]] = [
        (document, (), None, 0)
    ]
    while stack:
        node, segments, parent_key, depth = stack.pop()

        if not isinstance(node, (dict, list)):
            continue

        if depth > MAX_SCAN_DEPTH:
            container_reasons.setdefault(segments, SKIP_REASON_DEPTH)
            continue

        container_reasons.setdefault(segments, SKIP_REASON_NESTED)
        if not node:
            continue

        pending: list[tuple[Any, tuple[Segment, ...], str | None, int]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                child = segments + (key,)
                if isinstance(value, (dict, list)):
                    pending.append((value, child, key, depth + 1))
                else:
                    analyze_scalar(value, child, key)
        else:
            for index, value in enumerate(node):
                child = segments + (index,)
                if isinstance(value, (dict, list)):
                    pending.append((value, child, parent_key, depth + 1))
                elif parent_key is not None:
                    # Scalar list elements inherit the nearest field name.
                    analyze_scalar(value, child, parent_key)
                elif child not in seen_scalar:
                    seen_scalar.add(child)
                    scalar_skips.append((child, SKIP_REASON_FIELD, "field"))
        # Reverse-push so traversal order matches document order.
        stack.extend(reversed(pending))

    finding_prefixes: set[tuple[Segment, ...]] = set()
    for path in (finding.segments for finding in findings):
        for index in range(len(path)):
            finding_prefixes.add(path[:index])

    skipped: list[SkippedPath] = []
    skipped_set: set[tuple[Segment, ...]] = set()
    depth_limited = {
        path for path, reason in container_reasons.items() if reason == SKIP_REASON_DEPTH
    }
    depth_prefixes: set[tuple[Segment, ...]] = set()
    for path in depth_limited:
        for index in range(len(path)):
            depth_prefixes.add(path[:index])

    def ancestor_skipped(segments: tuple[Segment, ...]) -> bool:
        return any(
            segments[:index] in skipped_set for index in range(len(segments))
        )

    # Report each unknown subtree once, at its shallowest skipped container;
    # listing every descendant of an unknown subtree is pure noise.
    for path in sorted(container_reasons, key=len):
        if not path or path in finding_prefixes:
            continue
        if ancestor_skipped(path):
            continue
        skipped_set.add(path)
        skipped.append(
            SkippedPath(
                segments=path,
                reason=SKIP_REASON_DEPTH if path in depth_prefixes else container_reasons[path],
                kind="nested",
            )
        )

    # Children of a fully-skipped subtree add noise without review value.
    for segments, reason, kind in scalar_skips:
        if ancestor_skipped(segments):
            continue
        skipped.append(SkippedPath(segments=segments, reason=reason, kind=kind))

    return ScanResult(findings=findings, skipped=skipped)


def _set_at_path(document: Any, segments: Sequence[Segment], value: Any) -> None:
    parent = document
    for segment in segments[:-1]:
        parent = parent[segment]
    parent[segments[-1]] = value


def _strong_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(ch.islower() for ch in candidate)
            and any(ch.isupper() for ch in candidate)
            and any(ch.isdigit() for ch in candidate)
            and any(ch in "!@#$%^&*()-_=+" for ch in candidate)
        ):
            return candidate


def _deep_copy_json(document: Any) -> Any:
    """Iterative deep copy of a JSON document.

    ``copy.deepcopy`` recurses and can exhaust the stack on hostile input;
    this walker uses an explicit stack instead.
    """

    if not isinstance(document, (dict, list)):
        return document

    def new_container(node: Any) -> Any:
        return {} if isinstance(node, dict) else []

    root = new_container(document)
    stack: list[tuple[Any, Any]] = [(document, root)]
    while stack:
        source, target = stack.pop()
        if isinstance(source, dict):
            for key, value in source.items():
                if isinstance(value, (dict, list)):
                    child = new_container(value)
                    target[key] = child
                    stack.append((value, child))
                else:
                    target[key] = value
        else:
            for value in source:
                if isinstance(value, (dict, list)):
                    child = new_container(value)
                    target.append(child)
                    stack.append((value, child))
                else:
                    target.append(value)
    return root


def apply_fixes(
    document: Any, findings: Sequence[Finding]
) -> FixOutcome:
    """Return a fixed copy and a description of every substitution.

    Only values at finding paths are replaced; key order, nesting and all
    untouched values are preserved by the deep copy. Callers re-scan the
    returned document to verify the result.
    """

    fixed = _deep_copy_json(document)
    fixes: list[dict[str, Any]] = []
    fixed_paths: set[tuple[Segment, ...]] = set()

    ordered = sorted(
        findings, key=lambda item: (path_sort_key(item.segments), item.rule.rule_id)
    )
    for finding in ordered:
        if not finding.rule.fixable:
            continue
        if finding.segments in fixed_paths:
            # Two overlapping rules matched the same field; a single safe
            # replacement is enough and avoids one fix undoing another.
            continue
        if finding.rule.operator == "weak_password":
            replacement = _strong_password()
            action = "replaced_with_strong_random_password"
        elif finding.rule.operator == "plaintext_secret":
            replacement = VAULT_REFERENCE
            action = "replaced_with_vault_reference"
        else:  # defensive: fixable is derived from operator
            continue

        _set_at_path(fixed, finding.segments, replacement)
        fixed_paths.add(finding.segments)
        fixes.append(
            {
                "path": finding.path,
                "bare_path": finding.bare_path,
                "rule_id": finding.rule.rule_id,
                "rule_type": finding.rule.rule_type,
                "action": action,
            }
        )

    return FixOutcome(document=fixed, fixes=fixes)


def build_report(
    *,
    config_path: str,
    rule_result: RulesLoadResult,
    scan: ScanResult,
    mode: str,
    fixes: Sequence[dict[str, Any]] | None = None,
    fixed_config_path: str | None = None,
    pre_fix_total: int | None = None,
    pre_fix_findings: Sequence[Finding] | None = None,
) -> dict[str, Any]:
    """Build the deterministic report handed to ``--output``."""
    findings = [item.to_dict() for item in scan.sorted_findings()]
    skipped = [item.to_dict() for item in scan.sorted_skipped()]

    by_severity = {label: 0 for label in SEVERITY_LABELS.values()}
    by_type: dict[str, int] = {}
    for item in scan.findings:
        by_severity[item.rule.severity_label] += 1
        by_type[item.rule.rule_type] = by_type.get(item.rule.rule_type, 0) + 1

    report: dict[str, Any] = {
        "tool": "secscan",
        "version": "1.0.0",
        "mode": mode,
        "config": config_path,
        "rules": {
            "source": rule_result.source,
            "name": rule_result.name,
            "path": rule_result.path,
            "count": len(rule_result.rules),
        },
        "total": len(findings),
        "summary": {
            "by_severity": by_severity,
            "by_type": dict(sorted(by_type.items())),
            "skipped": len(skipped),
        },
        "vulnerabilities": findings,
        "skipped": skipped,
        "skipped_paths": [entry["path"] for entry in skipped],
        "skipped_paths_bare": [entry["bare_path"] for entry in skipped],
    }
    if fixes is not None:
        report["fixes"] = list(fixes)
        report["fixed_config"] = fixed_config_path
        report["pre_fix_total"] = pre_fix_total
        report["post_fix_total"] = len(findings)
        if pre_fix_findings is not None:
            report["pre_fix_findings"] = [
                item.to_dict() for item in pre_fix_findings
            ]
    return report
