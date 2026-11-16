"""Local orchestration of one origin and N edge node processes.

All processes run on loopback high ports and speak the JSON-lines protocol.
Startup failures (including occupied ports) raise E_PORT and every already
started process is cleaned up, leaving no orphans.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from .config import load_policy
from .errors import E_PORT, E_STALE, E_UPSTREAM, E_EVICT, CdnError
from .protocol import HOST, send_request
from .session import SessionStore

DEFAULT_BASE_PORT = 29301
NUM_EDGES = 3


def _port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_free_ports(count):
    ports = []
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((HOST, 0))
            ports.append(sock.getsockname()[1])
            sockets.append(sock)
    finally:
        for sock in sockets:
            sock.close()
    return ports


class Orchestrator:
    def __init__(self, config_path, content_dir, db_path, num_edges=NUM_EDGES,
                 base_port=DEFAULT_BASE_PORT, ports=None, log_path=None):
        self.config_path = config_path
        self.content_dir = content_dir
        self.policy = load_policy(config_path)  # E_CONFIG raised here
        self.num_edges = num_edges
        if ports is not None:
            self.ports = list(ports)
        else:
            self.ports = [base_port + i for i in range(num_edges + 1)]
        self.origin_port = self.ports[0]
        self.edge_ports = self.ports[1:]
        self.store = SessionStore(db_path)
        self.log_path = log_path
        self.processes = []
        self.tick = 0
        self._log_handle = open(log_path, "a", encoding="utf-8") if log_path else None

    # ---- structured logging ---------------------------------------------------
    def log_event(self, event, **fields):
        record = {"event": event, "tick": self.tick, **fields}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if self._log_handle:
            self._log_handle.write(line + "\n")
            self._log_handle.flush()
        return record

    # ---- lifecycle -------------------------------------------------------------
    def start(self):
        for port in self.ports:
            if not _port_free(port):
                self.log_event("start_failed", code=E_PORT, port=port,
                               message=f"loopback port {port} is occupied")
                raise CdnError(E_PORT, f"loopback port {port} is occupied",
                               location=f"port={port}")
        env = dict(os.environ)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
        commands = [
            [sys.executable, "-m", "cdnlab.origin",
             "--port", str(self.origin_port), "--content-dir", self.content_dir]
        ]
        for index, port in enumerate(self.edge_ports):
            commands.append([
                sys.executable, "-m", "cdnlab.edge",
                "--port", str(port), "--origin-port", str(self.origin_port),
                "--config", self.config_path, "--node-id", f"edge-{index}",
            ])
        try:
            for cmd in commands:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, env=env,
                )
                self.processes.append(proc)
                first = proc.stdout.readline().strip()
                if not first:
                    raise CdnError(E_PORT, f"process exited during startup: {' '.join(cmd)}")
                banner = json.loads(first)
                if not banner.get("ok"):
                    raise CdnError(banner.get("code", E_PORT),
                                   banner.get("message", "startup failed"))
            # readiness handshake
            self._wait_ready(self.origin_port)
            for port in self.edge_ports:
                self._wait_ready(port)
        except Exception:
            self.stop()
            raise
        self.store.open_session(self.policy.config_version, self.policy.to_dict())
        origin = send_request(self.origin_port, {"op": "state"})
        self.initial_origin = {"contents": origin["contents"], "versions": origin["versions"]}
        self.log_event("started", origin_port=self.origin_port, edge_ports=self.edge_ports,
                       config_version=self.policy.config_version)
        return self

    @staticmethod
    def _wait_ready(port, attempts=50):
        last_exc = None
        for _ in range(attempts):
            try:
                resp = send_request(port, {"op": "ping"}, timeout=1.0)
                if resp.get("ok"):
                    return
            except Exception as exc:  # not ready yet
                last_exc = exc
            time.sleep(0.05)
        raise CdnError(E_PORT, f"service on port {port} not ready: {last_exc}")

    def stop(self):
        for proc in self.processes:
            try:
                proc.terminate()
            except OSError:
                pass
        for proc in self.processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        if self.processes:
            self.log_event("stopped", count=len(self.processes))
        self.processes = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        self.store.close()
        if self._log_handle:
            self._log_handle.close()

    # ---- deterministic clock ----------------------------------------------------
    def advance(self, steps=1):
        self.tick += steps
        return self.tick

    # ---- request driving ---------------------------------------------------------
    def request(self, edge_index, path, fields=None, force_validate=False, advance=True):
        if advance:
            self.advance()
        port = self.edge_ports[edge_index]
        payload = {
            "op": "request", "path": path, "fields": fields or {},
            "tick": self.tick, "force_validate": force_validate,
        }
        result = send_request(port, payload)
        seq = self.store.log_request(
            self.tick, f"edge-{edge_index}", path,
            {"path": path, "fields": fields or {}, "force_validate": force_validate},
            result,
        )
        self.log_event("request", seq=seq, edge=edge_index, path=path,
                       outcome=result.get("outcome"), code=result.get("code"),
                       consistency=result.get("consistency"))
        return result

    # ---- fault injection -----------------------------------------------------------
    def inject_origin_update(self, path, content):
        """Inject an origin content update; recorded with business code E_STALE."""
        self.advance()
        resp = send_request(self.origin_port, {"op": "update", "path": path, "content": content})
        record = {"code": E_STALE, "path": path, "origin_version": resp.get("origin_version")}
        self.store.log_injection(self.tick, "origin_update",
                                 {"path": path, "content": content}, record)
        self.log_event("injection", type="origin_update", code=E_STALE, path=path)
        return record

    def inject_unreachable(self, value=True):
        """Inject origin unreachability; recorded with business code E_UPSTREAM."""
        self.advance()
        send_request(self.origin_port, {"op": "set_reachable", "value": not value})
        record = {"code": E_UPSTREAM, "unreachable": bool(value)}
        self.store.log_injection(self.tick, "set_unreachable",
                                 {"unreachable": bool(value)}, record)
        self.log_event("injection", type="set_unreachable", code=E_UPSTREAM, value=value)
        return record

    def inject_eviction_pressure(self, edge_index, path, count, fields_factory=None):
        """Inject eviction pressure: flood one node with distinct keys (E_EVICT)."""
        self.advance()
        record = {"code": E_EVICT, "edge": edge_index, "count": count}
        self.store.log_injection(self.tick, "eviction_pressure",
                                 {"edge": edge_index, "path": path, "count": count}, record)
        self.log_event("injection", type="eviction_pressure", code=E_EVICT,
                       edge=edge_index, count=count)
        results = []
        for i in range(count):
            fields = fields_factory(i) if fields_factory else {"query": f"filler-{i}"}
            results.append(self.request(edge_index, path, fields=fields))
        return results

    # ---- state capture / restore -------------------------------------------------
    def capture_state(self):
        origin = send_request(self.origin_port, {"op": "state"})
        edges = {}
        for index, port in enumerate(self.edge_ports):
            state = send_request(port, {"op": "cache_state", "tick": self.tick})
            edges[f"edge-{index}"] = state
        return {
            "tick": self.tick,
            "origin": {"contents": origin["contents"], "versions": origin["versions"]},
            "initial_origin": {
                "contents": dict(self.initial_origin["contents"]),
                "versions": dict(self.initial_origin["versions"]),
            },
            "edges": edges,
        }

    def save_snapshot(self, name):
        state = self.capture_state()
        state["trace"] = self.store.load_trace()
        snapshot_id = self.store.save_snapshot(
            name, self.policy.config_version, self.store._seq, state)
        self.log_event("snapshot_saved", name=name, snapshot_id=snapshot_id)
        return snapshot_id

    def restore_snapshot(self, snapshot_id):
        """Restore a snapshot. Config-version mismatch forces a wholesale
        replacement of cache state and trace (never an incremental merge)."""
        snapshot = self.store.load_snapshot(snapshot_id)
        if snapshot is None:
            raise CdnError("E_REPLAY", f"snapshot {snapshot_id} not found")
        state = snapshot["state"]
        config_match = snapshot["config_version"] == self.policy.config_version
        # Wholesale replacement regardless; the flag only decides replayability.
        send_request(self.origin_port, {
            "op": "restore",
            "contents": state["origin"]["contents"],
            "versions": state["origin"]["versions"],
        })
        for index, port in enumerate(self.edge_ports):
            node_state = state["edges"].get(f"edge-{index}", {"entries": [], "evictions": []})
            entries = [
                {"key": item["key"], "key_tuple": item["key_tuple"], "entry": item}
                for item in node_state["entries"]
            ]
            evicted_keys = [ev["key"] for ev in node_state.get("evictions", [])]
            send_request(port, {
                "op": "restore", "entries": entries,
                "evicted_keys": evicted_keys,
                "evictions": node_state.get("evictions", []),
            })
        self.tick = state["tick"]
        self.log_event("snapshot_restored", snapshot_id=snapshot_id,
                       config_match=config_match)
        return {"snapshot": snapshot, "config_match": config_match}
