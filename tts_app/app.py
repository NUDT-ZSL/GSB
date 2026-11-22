"""本地可交互专业应用：计算图拓扑 / 缓存状态 / 快照与失效 三视图。"""

import os

from .cache import ContentCache
from .config import load_config_file
from .dag import NODE_ORDER, Engine, Limits
from .errors import TtsError
from .snapshot import SnapshotStore

VIEWS = ("graph", "cache", "snapshot")


class TtsApp:
    def __init__(self, workspace, config_path, speakers, tasks, limits=None):
        self.workspace = workspace
        self.speakers = speakers
        self.tasks = tasks
        self.config = load_config_file(config_path)
        self.limits = limits or Limits(degrade=True)
        self.cache = ContentCache(os.path.join(workspace, "cache"),
                                  max_entries=self.limits.max_cache_entries,
                                  on_limit="evict")
        self.snapshots = SnapshotStore(os.path.join(workspace, "snapshots"))
        self.lineage_dir = os.path.join(workspace, "lineage")
        self.last_error = None
        self.last_run = None
        self.view = "graph"

    def _engine(self):
        return Engine(self.cache, self.speakers, self.config,
                      limits=self.limits, lineage_dir=self.lineage_dir)

    def render(self):
        snap = self.snapshots.current()
        snap_ver = snap["version"] if snap else "无"
        lines = [
            "=" * 56,
            " TTS 本地专业应用 | 视图: %s | 快照版本: %s" % (self.view, snap_ver),
            " 最近业务错误码: %s" % (self.last_error or "无"),
            "-" * 56,
        ]
        if self.view == "graph":
            lines.append(" 计算图拓扑:")
            for i, node in enumerate(NODE_ORDER):
                lines.append("   [%s]" % node)
                if i < len(NODE_ORDER) - 1:
                    lines.append("    | v")
        elif self.view == "cache":
            stats = self.cache.stats()
            lines.append(" 缓存条目: %d / %d"
                         % (stats["entries"], stats["max_entries"]))
            if self.last_run:
                lines.append(" 命中(复用)节点: %d"
                             % len(self.last_run["reused_nodes"]))
                lines.append(" 重算节点: %d"
                             % len(self.last_run["recomputed_nodes"]))
            if stats["degraded"]:
                lines.append(" 降级类别: %s" % ",".join(stats["degraded"]))
        else:
            lines.append(" 快照版本: %s" % snap_ver)
            if snap:
                lines.append(" 校验和: %s... 有效: %s"
                             % (snap["checksum"][:16], snap["checksum_valid"]))
            if self.last_run:
                affected = ",".join(self.last_run["affected_utterances"])
                lines.append(" 失效传播覆盖: %s" % (affected or "无"))
                degraded = ",".join(self.last_run["degraded"])
                lines.append(" 降级类别: %s" % (degraded or "无"))
        lines.append("=" * 56)
        return "\n".join(lines)

    def run_synthesis(self):
        try:
            self.last_run = self._engine().run_all(self.tasks)
            self.last_error = None
        except TtsError as exc:
            self.last_error = exc.code
            raise
        return self.last_run

    def commit_snapshot(self, version):
        data = {"config": self.config,
                "stats": self.cache.stats(),
                "affected": (self.last_run or {}).get("affected_utterances", [])}
        try:
            manifest = self.snapshots.commit(data, version)
            self.last_error = None
            return manifest
        except TtsError as exc:
            self.last_error = exc.code
            raise

    def switch_view(self, view):
        if view in VIEWS:
            self.view = view

    def interactive(self):
        menu = ("命令: 1=计算图视图 2=缓存视图 3=快照视图 "
                "r=触发合成演示 s=提交快照 q=退出")
        print(self.render())
        while True:
            try:
                cmd = input(menu + "\n> ").strip().lower()
            except EOFError:
                break
            if cmd == "q":
                break
            elif cmd in ("1", "2", "3"):
                self.switch_view(VIEWS[int(cmd) - 1])
            elif cmd == "r":
                run = self.run_synthesis()
                print("合成完成: 重算 %d 节点, 复用 %d 节点, 受影响 %s"
                      % (len(run["recomputed_nodes"]),
                         len(run["reused_nodes"]),
                         ",".join(run["affected_utterances"])))
            elif cmd == "s":
                current = self.snapshots.current()
                version = (current["version"] + 1) if current else 1
                manifest = self.commit_snapshot(version)
                print("快照已提交: 版本 %d 校验和 %s..."
                      % (manifest["version"], manifest["checksum"][:16]))
            else:
                print("未知命令")
            print(self.render())
