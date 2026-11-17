"""Parallel per-node distribution with global concurrency cap, per-attempt
timeout, policy-driven retries, and receipt convergence checks."""
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from .errors import E_CONVERGE, PlatformError


def _send_apply(port, msg, timeout_ms):
    deadline = time.monotonic() + timeout_ms / 1000.0
    with socket.create_connection(("127.0.0.1", port), timeout=timeout_ms / 1000.0) as s:
        s.settimeout(max(0.001, deadline - time.monotonic()))
        f = s.makefile("rw", encoding="utf-8", newline="\n")
        f.write(json.dumps(msg) + "\n")
        f.flush()
        line = f.readline()
        if not line:
            raise TimeoutError("node closed connection without a receipt")
        return json.loads(line)


class Distributor:
    def __init__(self, store=None, logger=None):
        self.store = store
        self.logger = logger

    def _deliver(self, session_id, node, port, slot, policy):
        """Same single-send semantics per attempt; retries re-send identically."""
        attempts = policy["retries"] + 1
        last_error = None
        for attempt in range(1, attempts + 1):
            msg = {"op": "apply", "node": node, "version": slot["version"],
                   "seq": attempt, "slice": slot["entries"],
                   "derive": slot["derive"]}
            try:
                receipt = _send_apply(port, msg, policy["timeout_ms"])
            except (OSError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if self.logger:
                    self.logger.log("delivery_retry", node=node, attempt=attempt,
                                    reason=last_error)
                continue
            receipt["attempt"] = attempt
            if self.store:
                self.store.record_receipt(session_id, node, attempt,
                                          receipt["version"], receipt["seq"],
                                          receipt["derived"])
            if self.logger:
                self.logger.log("receipt", node=node, attempt=attempt,
                                version=receipt["version"], seq=receipt["seq"],
                                kind="first" if attempt == 1 else "retry")
            return receipt
        return {"op": "error", "node": node, "error": last_error or "no receipt"}

    def run(self, session_id, plan, node_ports, policy):
        receipts = {}
        with ThreadPoolExecutor(max_workers=policy["max_concurrency"]) as pool:
            futures = {
                node: pool.submit(self._deliver, session_id, node,
                                  node_ports[node], plan[node], policy)
                for node in sorted(plan)
            }
            for node in sorted(futures):
                receipts[node] = futures[node].result()

        deviations = []
        for node in sorted(plan):
            expected = plan[node]["expected_derived"]
            receipt = receipts[node]
            actual = receipt.get("derived") if receipt.get("op") == "receipt" else None
            if actual != expected:
                deviations.append({"node": node, "expected": expected,
                                   "actual": actual,
                                   "reason": receipt.get("error", "digest mismatch")})
        if deviations:
            raise PlatformError(E_CONVERGE,
                                f"{len(deviations)} node(s) did not converge",
                                deviations=deviations)
        return receipts
