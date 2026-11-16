"""Parallel per-node dispatch scheduler with child-process orchestration.

The platform listens on 127.0.0.1 with an ephemeral port, spawns one child
process per registered node, and speaks JSON lines over loopback TCP.
Global concurrency and per-dispatch timeout are configurable; a timed-out
dispatch is retried with the same seq (identical single-dispatch
semantics), and retry receipts are recorded distinctly from first-attempt
receipts.
"""

import json
import os
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from .errors import E_NODE, PlatformError


class NodeCrashed(Exception):
    pass


class ProtocolError(Exception):
    pass


class LineConn:
    """Buffered JSON-lines connection that survives recv timeouts."""

    def __init__(self, sock, timeout):
        self.sock = sock
        self.timeout = timeout
        self.buf = b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def recv(self, timeout=None):
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise NodeCrashed("connection closed by peer")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line


class NodeHandle:
    def __init__(self, node_id, proc, conn):
        self.id = node_id
        self.proc = proc
        self.conn = conn
        self.degraded = False
        self.seq = 0
        self.lock = threading.Lock()


class Cluster:
    def __init__(self, store, node_ids, fail_modes=None, concurrency=4,
                 timeout=2.0, max_retries=2, accept_timeout=10.0):
        self.store = store
        self.node_ids = list(node_ids)
        self.fail_modes = fail_modes or {}
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.accept_timeout = accept_timeout
        self.handles = {}
        self.errors = {}
        self.listener = None
        self.procs = {}

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(len(self.node_ids))
        port = self.listener.getsockname()[1]

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_base = dict(os.environ)
        env_base["PYTHONPATH"] = root + os.pathsep + env_base.get("PYTHONPATH", "")
        for node_id in self.node_ids:
            env = dict(env_base)
            mode = self.fail_modes.get(node_id, "none")
            if mode != "none":
                env["NODE_FAIL_MODE"] = mode
                if mode == "slow":
                    env["NODE_DELAY"] = "0.6"
            self.procs[node_id] = subprocess.Popen(
                [sys.executable, "-m", "configdist.node_sim",
                 "--id", node_id, "--port", str(port)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        self.listener.settimeout(self.accept_timeout)
        pending = set(self.node_ids)
        while pending:
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                for node_id in sorted(pending):
                    self._degrade(node_id, "node did not connect within accept timeout")
                break
            conn = LineConn(sock, self.timeout)
            try:
                hello = json.loads(conn.recv().decode("utf-8"))
                node_id = hello.get("node")
                if hello.get("type") != "hello" or node_id not in pending:
                    raise ProtocolError("invalid hello: %r" % (hello,))
            except (ProtocolError, NodeCrashed, socket.timeout,
                    json.JSONDecodeError) as exc:
                self._degrade_unknown(pending, "bad hello: %s" % exc)
                continue
            pending.discard(node_id)
            self.handles[node_id] = NodeHandle(node_id, self.procs[node_id], conn)
            self.store.set_node_status(node_id, "ok")

    def _degrade(self, node_id, reason):
        self.errors[node_id] = {"code": E_NODE, "reason": reason}
        self.store.set_node_status(node_id, "degraded")

    def _degrade_unknown(self, pending, reason):
        target = None
        for node_id in sorted(pending):
            if self.procs[node_id].poll() is not None:
                target = node_id
                break
        if target is None:
            target = sorted(pending)[0]
        self._degrade(target, reason)
        pending.discard(target)

    def shutdown(self):
        for handle in self.handles.values():
            try:
                handle.conn.send({"type": "shutdown"})
            except OSError:
                pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self.listener:
            self.listener.close()

    def dispatch_all(self, plan, skip_nodes=()):
        """Dispatch every assignment in parallel; returns (receipts, errors)."""
        version = plan["version"]
        jobs = {
            node: items
            for node, items in plan["assignments"].items()
            if node not in skip_nodes
        }
        receipts, errors = {}, {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self._dispatch_node, node, items, version): node
                for node, items in jobs.items()
            }
            for future, node in futures.items():
                try:
                    receipts[node] = future.result()
                except PlatformError as exc:
                    errors[node] = exc.to_dict()
        for node, err in self.errors.items():
            errors.setdefault(node, err)
        return receipts, errors

    def _dispatch_node(self, node, items, version):
        handle = self.handles.get(node)
        if handle is None or handle.degraded:
            raise PlatformError(E_NODE, "node %s is degraded" % node,
                                details={"node": node})
        with handle.lock:
            handle.seq += 1
            seq = handle.seq
        self.store.add_observation(node, version, "dispatch_start",
                                   "seq=%d keys=%d" % (seq, len(items)))
        last_exc = None
        for attempt in range(1, self.max_retries + 2):
            try:
                receipt = self._attempt(handle, items, version, seq, attempt)
                kind = "ack" if attempt == 1 else "retry"
                self.store.add_receipt(node, version, seq, attempt, kind,
                                       receipt["digest"], receipt["keys"])
                self.store.add_observation(
                    node, version, "dispatch_end",
                    "seq=%d attempt=%d result=ok" % (seq, attempt))
                return {"node": node, "version": version, "seq": seq,
                        "attempt": attempt, "kind": kind,
                        "digest": receipt["digest"], "keys": receipt["keys"]}
            except socket.timeout:
                last_exc = PlatformError(
                    E_NODE, "node %s dispatch timeout (attempt %d)" % (node, attempt),
                    details={"node": node, "attempt": attempt})
                self.store.add_observation(
                    node, version, "dispatch_retry",
                    "seq=%d attempt=%d reason=timeout" % (seq, attempt))
                continue
            except (NodeCrashed, ProtocolError, OSError) as exc:
                last_exc = PlatformError(E_NODE, "node %s failed: %s" % (node, exc),
                                         details={"node": node})
                break
        handle.degraded = True
        self._degrade(node, last_exc.message)
        self.store.add_observation(node, version, "dispatch_end",
                                   "seq=%d result=error code=%s" % (seq, E_NODE))
        raise last_exc

    def _attempt(self, handle, items, version, seq, attempt):
        with handle.lock:
            handle.conn.send({"type": "apply", "version": version, "seq": seq,
                              "attempt": attempt, "items": items})
            while True:
                raw = handle.conn.recv()
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    raise ProtocolError("non-JSON line from node %s" % handle.id)
                if not isinstance(msg, dict) or msg.get("type") != "ack":
                    raise ProtocolError("unexpected message from node %s: %r"
                                        % (handle.id, msg))
                if msg.get("node") != handle.id or msg.get("version") != version \
                        or msg.get("seq") != seq:
                    raise ProtocolError("mismatched ack fields from node %s" % handle.id)
                if msg.get("attempt") != attempt:
                    self.store.add_receipt(handle.id, version, seq,
                                           msg.get("attempt") or 0, "stale",
                                           msg.get("digest", ""), msg.get("keys", []))
                    continue
                if not isinstance(msg.get("digest"), str) \
                        or not isinstance(msg.get("keys"), list):
                    raise ProtocolError("malformed ack payload from node %s" % handle.id)
                return msg
