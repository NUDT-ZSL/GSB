#!/usr/bin/env python3
"""Deterministic local demo orchestrator for the locklease middleware.

Runs entirely on the loopback interface using local subprocesses and local
sample files. No containers and no network targets beyond 127.0.0.1.

Scenarios (stable output order):
  1. normal acquire / E_LOCKED / E_VERSION / release
  2. dependency invalidation propagation on release
  3. client subprocess crash -> lease expiry -> server-side reclaim
  4. snapshot wholesale replacement restore + replay verification
Fault injection:
  --inject cycle|snapshot|port   -> E_DEPEND | E_SNAPSHOT | E_PORT
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
ART_DIR = ROOT / "artifacts"
SAMPLES = ROOT / "samples"
DEFAULT_ADDR = "127.0.0.1:7801"


def exe(name: str) -> str:
    return str(BIN_DIR / (name + (".exe" if os.name == "nt" else "")))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def reset_runtime() -> None:
    for d in (DATA_DIR, LOG_DIR, ART_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for base in (DATA_DIR, LOG_DIR):
        for f in base.glob("*"):
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()


def start_server(addr: str | None = None, graph: Path | None = None,
                 data_dir: Path | None = None, log_dir: Path | None = None,
                 capture: Path | None = None) -> subprocess.Popen:
    graph = graph or (SAMPLES / "locks.graph.json")
    data_dir = data_dir or DATA_DIR
    log_dir = log_dir or LOG_DIR
    addr = addr or DEFAULT_ADDR
    out = open(capture, "wb") if capture else subprocess.PIPE
    proc = subprocess.Popen(
        [exe("lockd"), "--addr", addr, "--data", str(data_dir),
         "--graph", str(graph), "--logs", str(log_dir)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=out, text=True)
    wait_port(addr)
    return proc


def wait_port(addr: str, timeout: float = 8.0) -> None:
    host, port = addr.split(":")
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.settimeout(0.3)
            if sk.connect_ex((host, int(port))) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"server did not open {addr}")


def stop_server(proc: subprocess.Popen, addr: str | None = None) -> None:
    if proc.poll() is None:
        ctl([exe("lockctl"), "--addr", addr or DEFAULT_ADDR, "shutdown"])
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def ctl(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                          timeout=15, check=check)


def run_batch(name: str) -> list[dict]:
    section(f"SCENARIO {name}: batch JSON-line client requests")
    proc = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR,
                "--batch", str(SAMPLES / "requests" / name)])
    responses = []
    for line in proc.stdout.splitlines():
        resp = json.loads(line)
        responses.append(resp)
        print(json.dumps(resp, sort_keys=True, ensure_ascii=False))
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return responses


def summarize(responses: list[dict], label: str) -> dict:
    codes: dict[str, int] = {}
    for r in responses:
        if not r.get("ok"):
            codes[r.get("code", "?")] = codes.get(r.get("code", "?"), 0) + 1
    summary = {"scenario": label, "requests": len(responses), "errors": codes}
    print("SUMMARY " + json.dumps(summary, sort_keys=True, ensure_ascii=False))
    return summary


def scenario_normal() -> list[dict]:
    return run_batch("scenario1_normal.jsonl")


def scenario_propagation() -> list[dict]:
    return run_batch("scenario2_propagation.jsonl")


def scenario_preempt() -> list[dict]:
    return run_batch("scenario4_preempt.jsonl")

def scenario_crash_recovery() -> dict:
    section("SCENARIO 3: client subprocess crash -> lease expiry -> reclaim")
    ttl_ms = 800
    # Two real client subprocesses acquire leases over loopback.
    clients = []
    for idx, key in enumerate(("db_replica", "cache_layer")):
        holder = f"crashed-client-{idx}"
        p = subprocess.Popen(
            [exe("lockctl"), "--addr", DEFAULT_ADDR, "acquire",
             f"key={key}", f"holder={holder}", f"ttl_ms={ttl_ms}"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        out, _ = p.communicate(timeout=10)
        resp = json.loads(out.strip())
        print("ACQUIRE " + json.dumps(resp, sort_keys=True, ensure_ascii=False))
        if not resp.get("ok"):
            raise RuntimeError(f"client {holder} failed to acquire {key}")
        clients.append((p, holder, key, resp["lease_version"]))

    # Forcefully terminate the clients WITHOUT sending release/exit messages.
    for proc, holder, key, ver in clients:
        proc.kill()
        proc.wait()
        print(f"KILLED holder={holder} key={key} lease_version={ver} "
              "(no release message sent)")

    # The server-side reaper must reclaim solely from wall-clock expiry.
    deadline = time.time() + 6
    reclaimed: list[dict] = []
    wanted_keys = {item[2] for item in clients}
    while time.time() < deadline and len(reclaimed) < len(clients):
        time.sleep(0.15)
        # Observe via table only: per-key query would lazily reap inside the
        # request handler and bypass the background reaper log we are verifying.
        tbl = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR, "passive_table"])
        active = {row["key"] for row in json.loads(tbl.stdout.strip()).get("locks", [])}
        for _, holder, key, ver in clients:
            if key not in active and key in wanted_keys and not any(r["key"] == key for r in reclaimed):
                reclaimed.append({"key": key, "holder": holder, "version": ver})
    print("RECLAIMED " + json.dumps(reclaimed, sort_keys=True))
    log_text = (LOG_DIR / "lockd.log").read_text(encoding="utf-8")
    wanted = {(item[2], item[1]) for item in clients}
    reclaim_lines = [ln for ln in log_text.splitlines()
                     if "reclaim" in ln and
                     any(f"key={k} holder={h}" in ln for k, h in wanted)]
    print("SERVER-RECLAIM-LOG")
    for ln in reclaim_lines:
        print("  " + ln)
    result = {"reclaimed": reclaimed, "reclaim_log": reclaim_lines}
    if len(reclaimed) != len(clients):
        raise RuntimeError("not all crashed-client leases were reclaimed")
    return result


def scenario_snapshot() -> dict:
    section("SCENARIO 4: snapshot wholesale replacement + replay verification")
    # Build some history including a propagation event.
    for req_file in ("scenario1_normal.jsonl", "scenario2_propagation.jsonl"):
        run_batch(req_file)
    snap = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR,
                "snapshot", "snapshot_id=session-A"])
    snap_resp = json.loads(snap.stdout.strip())
    print("SNAPSHOT " + json.dumps(snap_resp, sort_keys=True))
    snap_path = snap_resp["snapshot"]["path"]

    # Diverge the live table so versions no longer match the snapshot.
    div = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR, "acquire",
               "key=standalone", "holder=divergent", "ttl_ms=9000", "now_ms=9000000"])
    print("DIVERGE " + div.stdout.strip())

    restore = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR,
                   "restore", f"path={snap_path}"])
    restore_resp = json.loads(restore.stdout.strip())
    print("RESTORE " + json.dumps(restore_resp, sort_keys=True))
    if not restore_resp.get("ok"):
        raise RuntimeError("restore/replay failed")
    info = restore_resp["restore"]
    if not info["Replaced"]:
        raise RuntimeError("version mismatch was not detected as replacement")

    # The divergent key must be gone (wholesale replace, not incremental merge)
    # and new requests must still be served.
    q = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR, "query",
             "key=standalone", "now_ms=9000001"])
    qresp = json.loads(q.stdout.strip())
    print("POST-RESTORE-QUERY " + json.dumps(qresp, sort_keys=True))
    if qresp.get("lock") is not None:
        raise RuntimeError("incremental merge detected after restore")
    post = ctl([exe("lockctl"), "--addr", DEFAULT_ADDR, "acquire",
                "key=standalone", "holder=post-restore",
                "ttl_ms=2000", "now_ms=9001000"])
    post_resp = json.loads(post.stdout.strip())
    print("POST-RESTORE-ACQUIRE " + json.dumps(post_resp, sort_keys=True))
    if not post_resp.get("ok"):
        raise RuntimeError("server cannot serve new requests after restore")
    return {"snapshot": snap_resp["snapshot"], "restore": info}


def inject_cycle() -> dict:
    section("FAULT INJECTION: cyclic dependency graph -> E_DEPEND")
    cap = ART_DIR / "inject_cycle.stderr.log"
    proc = subprocess.Popen(
        [exe("lockd"), "--addr", "127.0.0.1:7811",
         "--data", str(DATA_DIR / "cycle"),
         "--graph", str(SAMPLES / "locks.graph.cycle.json"),
         "--logs", str(LOG_DIR / "cycle")],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _, err = proc.communicate(timeout=10)
    cap.write_text(err, encoding="utf-8")
    print(err.strip())
    resp = json.loads(err.strip())
    if resp.get("code") != "E_DEPEND":
        raise RuntimeError(f"expected E_DEPEND, got {resp}")
    return resp


def inject_snapshot() -> dict:
    section("FAULT INJECTION: corrupt snapshot file -> E_SNAPSHOT / E_REPLAY")
    srv = start_server(addr="127.0.0.1:7812", data_dir=DATA_DIR / "snapinj",
                       log_dir=LOG_DIR / "snapinj")
    try:
        good = ctl([exe("lockctl"), "--addr", "127.0.0.1:7812",
                    "snapshot", "snapshot_id=before-corrupt"])
        good_resp = json.loads(good.stdout.strip())
        path = Path(good_resp["snapshot"]["path"])
        corrupt = path.with_name("corrupt.json")
        corrupt.write_text("{broken-json", encoding="utf-8")
        r1 = ctl([exe("lockctl"), "--addr", "127.0.0.1:7812",
                  "restore", f"path={corrupt}"])
        resp1 = json.loads(r1.stdout.strip())
        print("CORRUPT-FILE " + json.dumps(resp1, sort_keys=True))
        if resp1.get("code") != "E_SNAPSHOT":
            raise RuntimeError(f"expected E_SNAPSHOT, got {resp1}")

        # Build a snapshot that actually contains a propagation event, then
        # tamper its trusted per-key version baseline so replay must fail.
        ctl([exe("lockctl"), "--addr", "127.0.0.1:7812", "acquire",
             "key=db_primary", "holder=w1", "ttl_ms=10000", "now_ms=5000000"])
        ctl([exe("lockctl"), "--addr", "127.0.0.1:7812", "acquire",
             "key=db_replica", "holder=r1", "ttl_ms=10000", "now_ms=5000100"])
        ctl([exe("lockctl"), "--addr", "127.0.0.1:7812", "release",
             "key=db_primary", "holder=w1", "lease_version=1", "now_ms=5000200"])
        good2 = ctl([exe("lockctl"), "--addr", "127.0.0.1:7812",
                     "snapshot", "snapshot_id=with-trace"])
        path2 = Path(json.loads(good2.stdout.strip())["snapshot"]["path"])
        snap = json.loads(path2.read_text(encoding="utf-8"))
        target = snap["propagation_traces"][0]["affected_key"]
        snap["key_versions"][target] = 999
        tampered = path.with_name("tampered.json")
        tampered.write_text(json.dumps(snap), encoding="utf-8")
        r2 = ctl([exe("lockctl"), "--addr", "127.0.0.1:7812",
                  "restore", f"path={tampered}"])
        resp2 = json.loads(r2.stdout.strip())
        print("TAMPERED-REPLAY " + json.dumps(resp2, sort_keys=True))
        if resp2.get("code") != "E_REPLAY":
            raise RuntimeError(f"expected E_REPLAY, got {resp2}")
        return {"corrupt": resp1, "tampered": resp2}
    finally:
        stop_server(srv)


def inject_port() -> dict:
    section("FAULT INJECTION: port already in use -> E_PORT")
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 7813))
    holder.listen(5)
    try:
        proc = subprocess.Popen(
            [exe("lockd"), "--addr", "127.0.0.1:7813",
             "--data", str(DATA_DIR / "port"),
             "--graph", str(SAMPLES / "locks.graph.json"),
             "--logs", str(LOG_DIR / "port")],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        _, err = proc.communicate(timeout=10)
        print(err.strip())
        resp = json.loads(err.strip())
        if resp.get("code") != "E_PORT":
            raise RuntimeError(f"expected E_PORT, got {resp}")
        return resp
    finally:
        holder.close()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", choices=["cycle", "snapshot", "port", "all"],
                    help="run a fault-injection case instead of the full demo")
    ap.add_argument("--addr", default=None)
    args = ap.parse_args()

    globals()["DEFAULT_ADDR"] = args.addr or DEFAULT_ADDR
    if args.inject:
        reset_runtime()
        report: dict = {"fault_injection": {}}
        targets = ["cycle", "snapshot", "port"] if args.inject == "all" else [args.inject]
        for name in targets:
            if name == "cycle":
                report["fault_injection"]["cycle"] = inject_cycle()
            elif name == "snapshot":
                report["fault_injection"]["snapshot"] = inject_snapshot()
            else:
                report["fault_injection"]["port"] = inject_port()
        (ART_DIR / "fault-injection-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8")
        print("\nREPORT artifacts/fault-injection-report.json")
        return 0

    reset_runtime()
    srv = start_server()
    summaries = []
    try:
        summaries.append(summarize(scenario_normal(), "normal-acquire-release"))
        summaries.append(summarize(scenario_propagation(), "dependency-propagation"))
        summaries.append(summarize(scenario_preempt(), "force-preemption"))
        crash = scenario_crash_recovery()
        snap = scenario_snapshot()
    finally:
        stop_server(srv)

    report = {
        "scenarios": summaries,
        "crash_recovery": crash,
        "snapshot_restore": snap,
        "stable_business_codes": {
            "duplicate_acquire": "E_LOCKED",
            "version_mismatch": "E_VERSION",
            "bad_dependency_graph": "E_DEPEND",
            "corrupt_snapshot": "E_SNAPSHOT",
            "replay_mismatch": "E_REPLAY",
            "port_unavailable": "E_PORT",
        },
    }
    out = ART_DIR / "demo-report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nREPORT {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
