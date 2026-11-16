"""Node / Pod manifest loading and strict validation.

Any invalid record aborts the whole load with E_MANIFEST (no partial
loading). Affinity rules may use ``keyRef`` indirection into the
manifest-level rule library; reference cycles are rejected with
E_AFFINITY.
"""

import json
import re
from dataclasses import dataclass, field

from .errors import AffinityError, ManifestError

TAINT_EFFECTS = {"NoSchedule", "PreferNoSchedule", "NoExecute"}
MATCH_OPERATORS = {"In", "NotIn", "Exists", "DoesNotExist"}
TOLERATION_OPERATORS = {"Equal", "Exists"}

_LABEL_NAME = r"[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?"
_LABEL_PREFIX = r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"
LABEL_KEY_RE = re.compile(rf"^({_LABEL_PREFIX}/)?{_LABEL_NAME}$")
LABEL_VALUE_RE = re.compile(rf"^({_LABEL_NAME})?$")


@dataclass(frozen=True)
class MatchTerm:
    key: str
    operator: str
    values: tuple = ()

    def matches(self, labels):
        present = self.key in labels
        if self.operator == "Exists":
            return present
        if self.operator == "DoesNotExist":
            return not present
        if not present:
            return False
        if self.operator == "In":
            return labels[self.key] in self.values
        if self.operator == "NotIn":
            return labels[self.key] not in self.values
        return False


@dataclass(frozen=True)
class Taint:
    key: str
    value: str
    effect: str


@dataclass(frozen=True)
class Toleration:
    key: str
    operator: str
    value: str
    effect: str

    def tolerates(self, taint):
        if self.effect and self.effect != taint.effect:
            return False
        if self.operator == "Exists":
            return self.key == taint.key
        return self.key == taint.key and self.value == taint.value


@dataclass
class Node:
    name: str
    labels: dict
    allocatable: dict
    allocated: dict
    taints: list
    images: object  # None -> statistics missing; otherwise list of dicts
    existing_pods: list


@dataclass
class Pod:
    name: str
    request: dict
    image: str
    labels: dict
    node_affinity_required: list = field(default_factory=list)
    node_affinity_preferred: list = field(default_factory=list)
    anti_affinity_required: list = field(default_factory=list)
    anti_affinity_preferred: list = field(default_factory=list)
    tolerations: list = field(default_factory=list)


def _fail(message, entry, field):
    raise ManifestError(message, entry=entry, field=field)


def _check_label_key(key, entry, field):
    if not isinstance(key, str) or not LABEL_KEY_RE.match(key):
        _fail(f"invalid label key {key!r}", entry, field)


def _check_labels(labels, entry, field):
    if not isinstance(labels, dict):
        _fail("labels must be an object", entry, field)
    for k, v in labels.items():
        _check_label_key(k, entry, f"{field}.{k}")
        if not isinstance(v, str) or not LABEL_VALUE_RE.match(v):
            _fail(f"invalid label value {v!r}", entry, f"{field}.{k}")


def _check_positive_int(value, entry, field, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"expected integer, got {value!r}", entry, field)
    if allow_zero:
        if value < 0:
            _fail(f"expected non-negative integer, got {value}", entry, field)
    elif value <= 0:
        _fail(f"expected positive integer, got {value}", entry, field)


def _check_resources(res, entry, field, positive):
    if not isinstance(res, dict):
        _fail("resources must be an object", entry, field)
    for res_key in ("cpu_millis", "memory_mib"):
        if res_key not in res:
            _fail(f"missing resource {res_key}", entry, f"{field}.{res_key}")
        _check_positive_int(res[res_key], entry, f"{field}.{res_key}",
                            allow_zero=not positive)


def _parse_term(raw, entry, field, rules, resolving):
    """Parse a match term, resolving keyRef indirections (cycle-safe)."""
    if not isinstance(raw, dict):
        _fail("match term must be an object", entry, field)
    if "keyRef" in raw:
        ref = raw["keyRef"]
        if ref not in rules:
            raise AffinityError(f"unknown rule reference {ref!r}",
                                entry=entry, field=f"{field}.keyRef")
        if ref in resolving:
            chain = " -> ".join(list(resolving) + [ref])
            raise AffinityError(f"cyclic rule reference: {chain}",
                                entry=entry, field=f"{field}.keyRef")
        return _parse_term(rules[ref], entry, f"rules.{ref}",
                           rules, resolving + (ref,))
    key = raw.get("key")
    _check_label_key(key, entry, f"{field}.key")
    op = raw.get("operator")
    if op not in MATCH_OPERATORS:
        _fail(f"invalid operator {op!r}", entry, f"{field}.operator")
    values = raw.get("values", [])
    if op in ("In", "NotIn"):
        if not isinstance(values, list) or not values:
            _fail("values must be a non-empty list", entry, f"{field}.values")
        for i, v in enumerate(values):
            if not isinstance(v, str) or not LABEL_VALUE_RE.match(v):
                _fail(f"invalid value {v!r}", entry, f"{field}.values[{i}]")
    return MatchTerm(key=key, operator=op, values=tuple(values))


def _parse_preferred(raw_list, entry, field, rules):
    out = []
    for i, item in enumerate(raw_list):
        f = f"{field}[{i}]"
        weight = item.get("weight")
        _check_positive_int(weight, entry, f"{f}.weight")
        if weight > 100:
            _fail("preferred weight must be <= 100", entry, f"{f}.weight")
        term = _parse_term(item.get("term"), entry, f"{f}.term", rules, ())
        out.append({"weight": weight, "term": term})
    return out


def _parse_anti_terms(raw_list, entry, field):
    out = []
    for i, item in enumerate(raw_list):
        f = f"{field}[{i}]"
        selector = item.get("selector")
        if not isinstance(selector, dict) or not selector:
            _fail("anti-affinity selector must be a non-empty object",
                  entry, f"{f}.selector")
        _check_labels(selector, entry, f"{f}.selector")
        topo = item.get("topology_key")
        _check_label_key(topo, entry, f"{f}.topology_key")
        out.append({"selector": dict(selector), "topology_key": topo})
    return out


def _parse_anti_preferred(raw_list, entry, field):
    out = []
    for i, item in enumerate(raw_list):
        f = f"{field}[{i}]"
        weight = item.get("weight")
        _check_positive_int(weight, entry, f"{f}.weight")
        if weight > 100:
            _fail("preferred weight must be <= 100", entry, f"{f}.weight")
        terms = _parse_anti_terms([item.get("term")], entry, f"{f}.term")
        out.append({"weight": weight, "term": terms[0]})
    return out


def load_nodes(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return parse_nodes_doc(doc)


def parse_nodes_doc(doc):
    version = doc.get("version", "nodes-unversioned")
    raw_nodes = doc.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        _fail("nodes must be a non-empty list", "<manifest>", "nodes")
    seen = set()
    nodes = []
    for i, raw in enumerate(raw_nodes):
        entry = f"nodes[{i}]"
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            _fail("node name must be a non-empty string", entry, "name")
        if name in seen:
            _fail(f"duplicate node name {name!r}", entry, "name")
        seen.add(name)
        labels = raw.get("labels", {})
        _check_labels(labels, entry, "labels")
        alloc = raw.get("allocatable")
        _check_resources(alloc, entry, "allocatable", positive=True)
        allocated = raw.get("allocated", {"cpu_millis": 0, "memory_mib": 0})
        _check_resources(allocated, entry, "allocated", positive=False)
        taints = []
        for j, t in enumerate(raw.get("taints", [])):
            f = f"taints[{j}]"
            _check_label_key(t.get("key"), entry, f"{f}.key")
            if t.get("effect") not in TAINT_EFFECTS:
                _fail(f"invalid taint effect {t.get('effect')!r}", entry,
                      f"{f}.effect")
            taints.append(Taint(key=t["key"], value=t.get("value", ""),
                                effect=t["effect"]))
        images = raw.get("images", None)
        if images is not None:
            for j, img in enumerate(images):
                if not isinstance(img.get("name"), str) or \
                        not isinstance(img.get("size_mb"), int):
                    _fail("image entries need name/size_mb", entry,
                          f"images[{j}]")
        existing = raw.get("existing_pods", [])
        for j, p in enumerate(existing):
            _check_labels(p.get("labels", {}), entry,
                          f"existing_pods[{j}].labels")
        nodes.append(Node(name=name, labels=dict(labels),
                          allocatable=dict(alloc), allocated=dict(allocated),
                          taints=taints,
                          images=[dict(x) for x in images]
                          if images is not None else None,
                          existing_pods=[dict(p) for p in existing]))
    nodes.sort(key=lambda n: n.name)
    return version, nodes


def load_pods(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return parse_pods_doc(doc)


def parse_pods_doc(doc):
    version = doc.get("version", "pods-unversioned")
    rules = doc.get("rules", {})
    raw_pods = doc.get("pods")
    if not isinstance(raw_pods, list) or not raw_pods:
        _fail("pods must be a non-empty list", "<manifest>", "pods")
    seen = set()
    pods = []
    for i, raw in enumerate(raw_pods):
        entry = f"pods[{i}]({raw.get('name', '?')})"
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            _fail("pod name must be a non-empty string", entry, "name")
        if name in seen:
            _fail(f"duplicate pod name {name!r}", entry, "name")
        seen.add(name)
        request = raw.get("request")
        _check_resources(request, entry, "request", positive=True)
        labels = raw.get("labels", {})
        _check_labels(labels, entry, "labels")
        image = raw.get("image", "")
        if not isinstance(image, str) or not image:
            _fail("pod image must be a non-empty string", entry, "image")
        na = raw.get("node_affinity", {})
        pa = raw.get("pod_anti_affinity", {})
        tolerations = []
        for j, t in enumerate(raw.get("tolerations", [])):
            f = f"tolerations[{j}]"
            _check_label_key(t.get("key"), entry, f"{f}.key")
            if t.get("operator", "Equal") not in TOLERATION_OPERATORS:
                _fail("invalid toleration operator", entry, f"{f}.operator")
            effect = t.get("effect", "")
            if effect and effect not in TAINT_EFFECTS:
                _fail(f"invalid toleration effect {effect!r}", entry,
                      f"{f}.effect")
            tolerations.append(Toleration(
                key=t["key"], operator=t.get("operator", "Equal"),
                value=t.get("value", ""), effect=effect))
        pods.append(Pod(
            name=name, request=dict(request), image=image,
            labels=dict(labels),
            node_affinity_required=[
                _parse_term(t, entry, f"node_affinity.required[{k}]",
                            rules, ())
                for k, t in enumerate(na.get("required", []))],
            node_affinity_preferred=_parse_preferred(
                na.get("preferred", []), entry,
                "node_affinity.preferred", rules),
            anti_affinity_required=_parse_anti_terms(
                pa.get("required", []), entry,
                "pod_anti_affinity.required"),
            anti_affinity_preferred=_parse_anti_preferred(
                pa.get("preferred", []), entry,
                "pod_anti_affinity.preferred"),
            tolerations=tolerations))
    return version, pods
