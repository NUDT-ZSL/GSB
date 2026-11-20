"""Deterministic gray-routing decision engine.

Determinism guarantees:
  - every object collection is iterated in sorted-by-id order
  - all hashing uses graymesh.hashing.stable_hash (sha256 based)
  - all timestamps come from the injected LogicalClock, never the wall clock
  - weight accumulation walks members/routes in a fixed sorted order, so the
    same request sequence yields bit-identical decisions on every run
"""

from .hashing import stable_hash


def request_key(request):
    """Stable identity of a request for hashing purposes."""
    if "key" in request:
        return str(request["key"])
    return str(request.get("seq", 0))


class DecisionEngine:
    def __init__(self, snapshot, clock, invalidation_events=None):
        self.snapshot = snapshot
        self.clock = clock
        self.invalidation_events = invalidation_events or []

    def _headers_match(self, route, headers):
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        matches = route["spec"].get("matches", [])
        if not matches:
            return True
        for match in matches:
            wanted = {str(k).lower(): str(v) for k, v in match["headers"].items()}
            if all(lowered.get(k) == v for k, v in wanted.items()):
                return True
        return False

    def _weighted_pick(self, items, namespace, req_key):
        """items: list of (id, weight) sorted by id. Fixed-order accumulation."""
        total = 0
        for _, weight in items:
            total += weight
        point = stable_hash("%s:%s" % (namespace, req_key)) % total
        cumulative = 0
        for oid, weight in items:
            cumulative += weight
            if point < cumulative:
                return oid, {"namespace": namespace, "point": point, "total": total}
        raise AssertionError("unreachable")  # pragma: no cover

    def decide(self, request):
        snap = self.snapshot
        started_ms = self.clock.now_ms()
        req_key = request_key(request)
        headers = request.get("headers", {})
        chain = {
            "request_seq": request.get("seq"),
            "version": snap.version,
            "rules_digest": snap.digest,
            "started_ms": started_ms,
            "request_key": req_key,
        }

        # 1. virtual service: sorted by id, first one whose hosts list matches
        #    the Host header (or the first overall when no host filter matches)
        services = sorted(
            (obj for obj in snap.objects.values() if obj["kind"] == "virtual_service"),
            key=lambda o: o["id"])
        if not services:
            chain["outcome"] = "error"
            chain["error_code"] = "E_REF_MISSING"
            chain["error_detail"] = "no virtual_service defined"
            return chain
        host = str(headers.get("host", ""))
        vs = next((s for s in services
                   if s["spec"].get("hosts") and host in s["spec"]["hosts"]), services[0])
        chain["virtual_service_id"] = vs["id"]

        # 2. route selection: gray windows (per-VS bucket) -> header gate ->
        #    weighted fallback over header-matching routes
        route_ids = sorted(vs["spec"]["route_ids"])
        bucket = stable_hash("gray:%s:%s" % (vs["id"], req_key)) % 100
        chain["gray_bucket"] = bucket
        cumulative = 0
        gated = None
        gate_log = []
        for rid in route_ids:
            route = snap.get("gray_route", rid)
            percent = route["spec"]["gray_percent"]
            low, high = cumulative, cumulative + percent
            cumulative = high
            in_window = low <= bucket < high
            header_ok = self._headers_match(route, headers)
            gate_log.append({"route_id": rid, "window": [low, high],
                             "in_window": in_window, "header_match": header_ok})
            if gated is None and in_window and header_ok:
                gated = rid
        chain["route_gates"] = gate_log
        if gated is not None:
            route_id = gated
            chain["route_selected_by"] = "gray_window"
            chain["route_pick"] = None
        else:
            eligible = [
                (rid, snap.get("gray_route", rid)["spec"]["weight"])
                for rid in route_ids
                if self._headers_match(snap.get("gray_route", rid), headers)
            ]
            if not eligible:
                chain["outcome"] = "error"
                chain["error_code"] = "E_REF_MISSING"
                chain["error_detail"] = "no eligible route"
                return chain
            route_id, pick = self._weighted_pick(
                eligible, "route:%s" % vs["id"], req_key)
            chain["route_selected_by"] = "weight_fallback"
            chain["route_pick"] = pick
        chain["route_id"] = route_id
        route = snap.get("gray_route", route_id)

        # 3. upstream member selection
        group_id = route["spec"]["upstream_group_id"]
        group = snap.get("upstream_group", group_id)
        chain["upstream_group_id"] = group_id
        members = sorted(
            ((mid, snap.get("upstream_member", mid)["spec"]["weight"])
             for mid in group["spec"]["member_ids"]),
            key=lambda item: item[0])
        member_id, mpick = self._weighted_pick(members, "member:%s" % group_id, req_key)
        chain["member_id"] = member_id
        chain["member_pick"] = mpick

        # 4. mirror sampling (decision only; execution lives in the data plane)
        policy_id = route["spec"].get("mirror_policy_id")
        if policy_id:
            policy = snap.get("mirror_policy", policy_id)
            bucket = stable_hash("mirror:%s:%s" % (policy_id, req_key)) % 100
            sampled = bucket < policy["spec"]["percent"]
            mirror = {
                "policy_id": policy_id,
                "percent": policy["spec"]["percent"],
                "bucket": bucket,
                "sampled": sampled,
                "async": policy["spec"]["async"],
                "drop_response": policy["spec"]["drop_response"],
                "shadow_group_id": policy["spec"]["shadow_upstream_group_id"],
            }
            if sampled:
                shadow_group = snap.get(
                    "upstream_group", policy["spec"]["shadow_upstream_group_id"])
                shadow_members = sorted(
                    ((mid, snap.get("upstream_member", mid)["spec"]["weight"])
                     for mid in shadow_group["spec"]["member_ids"]),
                    key=lambda item: item[0])
                shadow_id, spick = self._weighted_pick(
                    shadow_members, "shadow:%s" % shadow_group["id"], req_key)
                mirror["shadow_member_id"] = shadow_id
                mirror["shadow_pick"] = spick
            chain["mirror"] = mirror
        else:
            chain["mirror"] = None

        # 5. invalidation provenance for diagnosability
        if self.invalidation_events:
            latest = self.invalidation_events[-1]
            chain["invalidation"] = {
                "patch_id": latest["trigger"]["patch_id"],
                "route_invalidated": ("gray_route/%s" % route_id)
                                     in latest["invalidated"]["parse_cache"],
                "reused": latest["reused"],
            }
        return chain
