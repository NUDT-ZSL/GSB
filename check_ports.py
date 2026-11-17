"""Check that every scene loopback port can be bound."""

from __future__ import annotations

import argparse
import json
import socket

from failover_console.scene import SceneLoader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查场景回环端口占用")
    parser.add_argument("--scene", required=True)
    parser.add_argument("--port-map", action="append", default=[], metavar="INSTANCE=PORT")
    args = parser.parse_args(argv)
    scene = SceneLoader().load(args.scene)
    overrides: dict[str, int] = {}
    for item in args.port_map:
        instance_id, separator, port_text = item.partition("=")
        if not separator:
            parser.error(f"无效端口映射: {item}")
        try:
            overrides[instance_id] = int(port_text)
        except ValueError:
            parser.error(f"无效端口映射: {item}")
    if overrides:
        scene = scene.with_port_overrides(overrides)
    results = []
    all_free = True
    for instance in scene.instances():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", instance.port))
            free = True
        except OSError as exc:
            free = False
            all_free = False
            results.append(
                {
                    "instance_id": instance.instance_id,
                    "port": instance.port,
                    "free": False,
                    "error": str(exc),
                }
            )
        else:
            results.append(
                {"instance_id": instance.instance_id, "port": instance.port, "free": True}
            )
        finally:
            sock.close()
    print(json.dumps({"ok": all_free, "ports": results}, ensure_ascii=False, indent=2))
    if not all_free:
        print("提示：可用 --port-map INSTANCE=PORT 或修改场景文件改用其它高位端口")
    return 0 if all_free else 1


if __name__ == "__main__":
    raise SystemExit(main())
