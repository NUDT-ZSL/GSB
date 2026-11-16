"""Receipt convergence and derived-state consistency checks (E_CONVERGE)."""

from .digest import canonical_digest


def expected_digests(plan):
    """Platform-recomputed expected derived-state digest per node."""
    return {
        node: canonical_digest(items)
        for node, items in plan["assignments"].items()
    }


def latest_receipts(receipts):
    """Pick the highest-attempt receipt per node."""
    latest = {}
    for receipt in receipts:
        node = receipt["node"]
        if node not in latest or receipt["attempt"] > latest[node]["attempt"]:
            latest[node] = receipt
    return latest


def check_convergence(plan, receipts):
    """Compare node receipts against platform-recomputed digests.

    A session is complete only when every target node acknowledged with a
    matching derived-state digest; partial success is never completion.
    """
    version = plan["version"]
    expected = expected_digests(plan)
    latest = latest_receipts(receipts)

    missing, mismatched = [], []
    for node in sorted(expected):
        receipt = latest.get(node)
        if receipt is None:
            missing.append(node)
        elif receipt["digest"] != expected[node]:
            mismatched.append(node)

    ok = not missing and not mismatched
    result = {
        "ok": ok,
        "version": version,
        "acked": sorted(latest),
        "missing": missing,
        "mismatched": mismatched,
        "expected": expected,
    }
    if not ok:
        result["code"] = "E_CONVERGE"
        result["deviations"] = [
            {"node": n, "version": version, "kind": "missing"} for n in missing
        ] + [
            {"node": n, "version": version, "kind": "digest_mismatch"}
            for n in mismatched
        ]
    return result
