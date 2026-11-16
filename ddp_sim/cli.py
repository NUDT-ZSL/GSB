"""Command line interface: run / verify / demo / gui / ports."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

from .config import DEFAULT_BASE_PORT, SimConfig, load_config_file
from .controller import Controller, verify_run
from .errors import EXIT_CODES, SimError
from .snapshot import load_snapshot


def _emit_error(exc):
    print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
    return EXIT_CODES.get(exc.code, 1)


def _parse_delays(pairs):
    delays = {}
    for pair in pairs or []:
        rank, _, ms = pair.partition("=")
        delays[int(rank)] = float(ms)
    return delays


def _build_config(args):
    if getattr(args, "config", None):
        cfg = load_config_file(args.config)
    else:
        cfg = SimConfig()
    for attr in ("workers", "dim", "seed", "block_size", "interleave",
                 "base_port", "lr"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, attr, value)
    delays = _parse_delays(getattr(args, "delay", None))
    if delays:
        cfg.delays_ms.update(delays)
    if getattr(args, "log_dir", None):
        cfg.log_dir = args.log_dir
    cfg.validate()
    return cfg


def _print_step(result):
    print(json.dumps({
        "event": "step", "version": result.version,
        "grad_digest": result.grad_digest,
        "param_digest": result.param_digest,
        "elapsed_s": round(result.elapsed, 4),
    }, sort_keys=True))


def cmd_run(args):
    cfg = _build_config(args)
    controller = Controller(cfg)

    held = None
    if args.inject == "port-busy":
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", cfg.base_port))
        held.listen(1)
    if args.inject == "bad-block":
        cfg.block_size = cfg.dim + 1
        cfg.validate()  # raises E_BLOCK
    if args.inject == "corrupt-snapshot":
        path = args.restore or args.snapshot or "snapshot.json"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not a valid snapshot ")
        args.restore = path

    try:
        if args.restore:
            info = controller.restore(args.restore)
            print(json.dumps({"event": "restore", **info}, sort_keys=True))
        for _ in range(args.steps):
            result = controller.run_step()
            _print_step(result)
            if args.snapshot:
                checksum = controller.save_snapshot(args.snapshot)
                print(json.dumps({"event": "snapshot", "path": args.snapshot,
                                  "version": controller.version,
                                  "checksum": checksum}, sort_keys=True))
            if args.step_pause:
                time.sleep(args.step_pause)
    finally:
        if held is not None:
            held.close()
    return 0


def cmd_verify(args):
    info = verify_run(args.log_dir)
    print(json.dumps({"event": "verify", **info}, sort_keys=True))
    return 0


def cmd_ports(args):
    cfg = _build_config(args)
    busy = []
    for port in cfg.ports():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            busy.append(port)
        finally:
            sock.close()
    print(json.dumps({"event": "ports", "range": [cfg.base_port,
                      cfg.base_port + cfg.workers - 1], "busy": busy},
                     sort_keys=True))
    return 1 if busy else 0


def cmd_demo(args):
    """Three reproducible scenarios with stable output ordering."""
    log_dir = args.log_dir
    snap = os.path.join(log_dir, "demo-snapshot.json")

    def run_once(delays, tag):
        cfg = SimConfig(workers=4, dim=256, seed=20240501, block_size=64,
                        interleave=2, base_port=args.base_port,
                        delays_ms=delays, log_dir=log_dir)
        result = Controller(cfg).run_step()
        print(json.dumps({"scenario": tag, "version": result.version,
                          "grad_digest": result.grad_digest,
                          "param_digest": result.param_digest}, sort_keys=True))
        return result

    print("== scenario 1: normal training step ==")
    normal = run_once({}, "normal")
    print("== scenario 2: training step with injected delays ==")
    delayed = run_once({0: 60.0, 1: 30.0, 2: 90.0, 3: 15.0}, "delayed")
    same = normal.grad_digest == delayed.grad_digest
    print(json.dumps({"scenario": "delay-invariance",
                      "grad_digest_equal": same}, sort_keys=True))
    if not same:
        raise SimError("E_DIVERGE", "delay injection changed the reduction result")

    print("== scenario 3: snapshot, kill -9, restore ==")
    if os.path.exists(snap):
        os.remove(snap)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ddp_sim", "run", "--workers", "4", "--dim", "256",
         "--seed", "20240501", "--block-size", "64", "--steps", "50",
         "--base-port", str(args.base_port), "--snapshot", snap,
         "--step-pause", "0.4", "--log-dir", log_dir],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if os.path.exists(snap):
            try:
                if load_snapshot(snap)["version"] >= 2:
                    break
            except SimError:
                pass
        time.sleep(0.1)
    proc.kill()  # SIGKILL: no exit reports promised
    proc.wait()
    snap_state = load_snapshot(snap)
    print(json.dumps({"scenario": "killed", "snapshot_version": snap_state["version"],
                      "steps_done": snap_state["steps_done"]}, sort_keys=True))
    cfg = SimConfig(workers=4, dim=256, seed=20240501, block_size=64,
                    interleave=2, base_port=args.base_port, log_dir=log_dir)
    controller = Controller(cfg)
    info = controller.restore(snap)
    print(json.dumps({"scenario": "restored", **info}, sort_keys=True))
    result = controller.run_step()
    print(json.dumps({"scenario": "resumed-step", "version": result.version,
                      "grad_digest": result.grad_digest,
                      "param_digest": result.param_digest}, sort_keys=True))
    return 0


def cmd_gui(args):
    from .gui import run_gui
    cfg = _build_config(args)
    run_gui(cfg)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="ddp_sim",
                                     description="Local ring all-reduce simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--config", help="JSON seed/config file")
        p.add_argument("--workers", type=int)
        p.add_argument("--dim", type=int)
        p.add_argument("--seed", type=int)
        p.add_argument("--block-size", type=int)
        p.add_argument("--interleave", type=int)
        p.add_argument("--base-port", type=int)
        p.add_argument("--lr", type=float)
        p.add_argument("--log-dir", default=None)

    run = sub.add_parser("run", help="run training step(s)")
    common(run)
    run.add_argument("--delay", action="append", metavar="RANK=MS",
                     help="inject fixed send delay per worker")
    run.add_argument("--steps", type=int, default=1)
    run.add_argument("--snapshot", help="save snapshot after each step")
    run.add_argument("--restore", help="restore snapshot before running")
    run.add_argument("--step-pause", type=float, default=0.0)
    run.add_argument("--inject", choices=["port-busy", "bad-block",
                                          "corrupt-snapshot"])
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser("verify", help="check gradient consistency of a run")
    verify.add_argument("--log-dir", required=True)
    verify.set_defaults(func=cmd_verify)

    ports = sub.add_parser("ports", help="report loopback port range status")
    common(ports)
    ports.set_defaults(func=cmd_ports)

    demo = sub.add_parser("demo", help="run the three reproducible scenarios")
    demo.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    demo.add_argument("--log-dir", default="runs/demo")
    demo.set_defaults(func=cmd_demo)

    gui = sub.add_parser("gui", help="start the Tkinter interface")
    common(gui)
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SimError as exc:
        return _emit_error(exc)


if __name__ == "__main__":
    sys.exit(main())
