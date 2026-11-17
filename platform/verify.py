"""Independent post-recovery consistency verifier. Reads only the SQLite
session DB, recomputes the distribution plan and expected derived digests for
the most recently committed version, and compares them item-by-item against
the stored plan and receipts. Non-reproducible states raise E_REPLAY."""
import json

from . import manifest as mf
from .errors import E_REPLAY, PlatformError
from .plan import compile_plan
from .store import Store


def verify_db(db_path):
    store = Store(db_path)
    try:
        committed = store.last_committed()
        if committed is None:
            raise PlatformError(E_REPLAY, "no committed version in session DB",
                                basis="committed table is empty")
        version = committed["version"]
        session_id = committed["session_id"]

        raw = store.get_meta(f"manifest_v{version}")
        if raw is None:
            raise PlatformError(
                E_REPLAY,
                f"config manifest for committed version {version} is missing; "
                "plan cannot be reproduced",
                basis=f"meta key 'manifest_v{version}' absent")
        manifest = mf.loads(raw)

        stored_plan = store.get_plan(session_id)
        if not stored_plan:
            raise PlatformError(E_REPLAY, "stored plan is empty",
                                basis=f"no plan rows for session {session_id}")
        recomputed = compile_plan(manifest, sorted(stored_plan))

        plan_diffs = []
        for node in sorted(stored_plan):
            got, want = stored_plan[node], recomputed[node]
            if got["entries"] != want["entries"]:
                plan_diffs.append({"node": node, "field": "entries"})
            if got["expected_derived"] != want["expected_derived"]:
                plan_diffs.append({
                    "node": node, "field": "expected_derived",
                    "expected": want["expected_derived"],
                    "actual": got["expected_derived"]})
        if plan_diffs:
            raise PlatformError(E_REPLAY,
                                "stored plan diverges from recomputation",
                                basis="plan mismatch", diffs=plan_diffs)

        receipts = store.get_receipts(session_id)
        receipt_nodes = {r["node"] for r in receipts}
        downgraded = sorted({r["node"] for r in receipts
                             if r["version"] < stored_plan[r["node"]]["version"]})
        if downgraded:
            raise PlatformError(
                E_REPLAY, "node downgrade detected; state not reproducible",
                basis=f"receipt version below committed version {version}",
                nodes=downgraded)
        missing = sorted(set(stored_plan) - receipt_nodes)
        digest_mismatch = sorted(
            {"node": r["node"], "expected": stored_plan[r["node"]]["expected_derived"],
             "actual": r["derived"]}
            for r in receipts
            if r["derived"] != stored_plan[r["node"]]["expected_derived"])

        summary = {
            "status": "ok" if not (missing or digest_mismatch) else "inconsistent",
            "committed_version": version,
            "session_id": session_id,
            "manifest_digest": mf.canonical_digest(manifest),
            "nodes": {node: {
                "version": stored_plan[node]["version"],
                "expected_derived": stored_plan[node]["expected_derived"],
                "receipts": [{"attempt": r["attempt"], "seq": r["seq"],
                              "version": r["version"], "derived": r["derived"]}
                             for r in receipts if r["node"] == node],
            } for node in sorted(stored_plan)},
            "missing_receipts": missing,
            "digest_mismatches": digest_mismatch,
        }
        return summary
    finally:
        store.close()


def main(argv=None):
    import argparse
    import sys
    p = argparse.ArgumentParser(description="verify a recovered session DB")
    p.add_argument("--db", required=True)
    args = p.parse_args(argv)
    try:
        summary = verify_db(args.db)
    except PlatformError as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
