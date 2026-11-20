"""Declarative rule object model, validation and dependency graph.

Five kinds of objects, referencing each other by id:
  virtual_service -> gray_route -> upstream_group -> upstream_member
  gray_route -> mirror_policy -> upstream_group (shadow)
upstream_group may optionally reference a fallback upstream_group, which makes
real cycles possible; they are detected and rejected with E_CYCLE.
"""

from .errors import BusinessError, ErrorCode

KINDS = ("virtual_service", "gray_route", "upstream_group", "upstream_member", "mirror_policy")


def key_of(obj):
    return (obj["kind"], obj["id"])


def _fail(message, details=None):
    raise BusinessError(ErrorCode.FIELD_INVALID, message, details)


def _require(cond, message, details=None):
    if not cond:
        _fail(message, details)


def _is_pos_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_percent(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def validate_object(obj):
    """Field-level validation of a single object. Raises BusinessError."""
    if not isinstance(obj, dict):
        _fail("object must be a dict")
    kind = obj.get("kind")
    oid = obj.get("id")
    spec = obj.get("spec")
    _require(kind in KINDS, "unknown kind", {"kind": kind})
    _require(isinstance(oid, str) and oid, "id must be a non-empty string", {"kind": kind})
    _require(isinstance(spec, dict), "spec must be a dict", {"id": oid})

    if kind == "upstream_member":
        _require(isinstance(spec.get("host"), str) and spec["host"], "member.host required", {"id": oid})
        port = spec.get("port")
        _require(isinstance(port, int) and 1 <= port <= 65535, "member.port must be 1..65535", {"id": oid})
        _require(_is_pos_int(spec.get("weight")), "member.weight must be a positive integer", {"id": oid})
    elif kind == "upstream_group":
        members = spec.get("member_ids")
        _require(isinstance(members, list) and members and all(isinstance(m, str) for m in members),
                 "group.member_ids must be a non-empty list of strings", {"id": oid})
        _require(spec.get("lb", "weighted") == "weighted", "group.lb must be 'weighted'", {"id": oid})
        fb = spec.get("fallback_group_id")
        _require(fb is None or isinstance(fb, str), "group.fallback_group_id must be a string", {"id": oid})
    elif kind == "gray_route":
        _require(isinstance(spec.get("upstream_group_id"), str) and spec["upstream_group_id"],
                 "route.upstream_group_id required", {"id": oid})
        _require(_is_pos_int(spec.get("weight")), "route.weight must be a positive integer", {"id": oid})
        _require(_is_percent(spec.get("gray_percent")), "route.gray_percent must be an integer in [0,100]",
                 {"id": oid})
        matches = spec.get("matches", [])
        _require(isinstance(matches, list), "route.matches must be a list", {"id": oid})
        for match in matches:
            headers = match.get("headers") if isinstance(match, dict) else None
            _require(isinstance(headers, dict)
                     and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()),
                     "route.matches[].headers must be a string map", {"id": oid})
        mp = spec.get("mirror_policy_id")
        _require(mp is None or isinstance(mp, str), "route.mirror_policy_id must be a string", {"id": oid})
    elif kind == "mirror_policy":
        _require(isinstance(spec.get("shadow_upstream_group_id"), str) and spec["shadow_upstream_group_id"],
                 "mirror.shadow_upstream_group_id required", {"id": oid})
        _require(_is_percent(spec.get("percent")), "mirror.percent must be an integer in [0,100]", {"id": oid})
        _require(isinstance(spec.get("async"), bool), "mirror.async must be a boolean", {"id": oid})
        _require(isinstance(spec.get("drop_response"), bool), "mirror.drop_response must be a boolean",
                 {"id": oid})
    elif kind == "virtual_service":
        _require(isinstance(spec.get("name"), str) and spec["name"], "vs.name required", {"id": oid})
        routes = spec.get("route_ids")
        _require(isinstance(routes, list) and routes and all(isinstance(r, str) for r in routes),
                 "vs.route_ids must be a non-empty list of strings", {"id": oid})
    return obj


def references_of(obj):
    """Return list of (kind, id) this object references."""
    kind, spec = obj["kind"], obj["spec"]
    if kind == "virtual_service":
        return [("gray_route", rid) for rid in spec["route_ids"]]
    if kind == "gray_route":
        refs = [("upstream_group", spec["upstream_group_id"])]
        if spec.get("mirror_policy_id"):
            refs.append(("mirror_policy", spec["mirror_policy_id"]))
        return refs
    if kind == "upstream_group":
        refs = [("upstream_member", m) for m in spec["member_ids"]]
        if spec.get("fallback_group_id"):
            refs.append(("upstream_group", spec["fallback_group_id"]))
        return refs
    if kind == "mirror_policy":
        return [("upstream_group", spec["shadow_upstream_group_id"])]
    return []


def build_dependency_graph(objects):
    """objects: dict key->obj. Returns (forward, reverse) adjacency maps."""
    forward = {key: [] for key in objects}
    reverse = {key: [] for key in objects}
    for key, obj in objects.items():
        for ref in references_of(obj):
            if ref not in objects:
                raise BusinessError(
                    ErrorCode.REF_MISSING,
                    "object %s/%s references missing %s/%s" % (key[0], key[1], ref[0], ref[1]),
                    {"from": "%s/%s" % key, "to": "%s/%s" % ref},
                )
            forward[key].append(ref)
            reverse[ref].append(key)
    return forward, reverse


def detect_cycle(forward):
    """DFS cycle detection. Raises BusinessError(E_CYCLE) with the cycle path."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {key: WHITE for key in forward}
    stack = []

    def visit(node):
        color[node] = GRAY
        stack.append(node)
        for nxt in forward[node]:
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycle = ["%s/%s" % k for k in stack[idx:]] + ["%s/%s" % nxt]
                raise BusinessError(ErrorCode.CYCLE, "dependency cycle detected", {"cycle": cycle})
            if color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for key in sorted(forward):
        if color[key] == WHITE:
            visit(key)


def validate_all(objects):
    """Full validation: fields, reference closure, cycles, gray-percent sums.

    Raises BusinessError; on success returns the objects dict unchanged.
    """
    for key in sorted(objects):
        obj = objects[key]
        if key_of(obj) != key:
            _fail("object key mismatch", {"key": "%s/%s" % key, "object": "%s/%s" % key_of(obj)})
        validate_object(obj)
    forward, _ = build_dependency_graph(objects)
    detect_cycle(forward)
    # gray percent sum per virtual service must not exceed 100
    for key in sorted(objects):
        obj = objects[key]
        if obj["kind"] != "virtual_service":
            continue
        total = 0
        for rid in obj["spec"]["route_ids"]:
            total += objects[("gray_route", rid)]["spec"]["gray_percent"]
        if total > 100:
            raise BusinessError(
                ErrorCode.FIELD_INVALID,
                "sum of gray_percent in virtual_service %s exceeds 100" % obj["id"],
                {"virtual_service": obj["id"], "sum": total},
            )
    return objects


def objects_from_list(obj_list):
    objects = {}
    for obj in obj_list:
        validate_object(obj)
        key = key_of(obj)
        if key in objects:
            _fail("duplicate object id", {"key": "%s/%s" % key})
        objects[key] = obj
    return objects
