"""Local interactive workbench (terminal REPL).

Displays the mask view (with viewport panning to simulate dragging),
per-pixel / per-instance query results, latency observations, metric
details, the snapshot version and the most recent business error codes.
Type ``help`` inside the REPL for the command list.
"""
from __future__ import annotations

import json

from .config import WorkbenchConfig
from .errors import PanoError
from .session import Session

CATEGORY_GLYPHS = " .#@%&*+=?"   # index 0 = ignore/background


class Workbench:
    def __init__(self, cfg: WorkbenchConfig):
        self.session = Session(cfg)
        self.session.run_pipeline()
        self.vx = 0               # viewport origin (drag state)
        self.vy = 0
        self.vw = min(32, cfg.width)
        self.vh = min(16, cfg.height)

    # ------------------------------------------------------------- rendering
    def render_view(self) -> str:
        mask = self.session.mask
        lines = [
            f"viewport origin=({self.vx},{self.vy}) size={self.vw}x{self.vh} "
            f"(pan with: pan <dx> <dy>)"
        ]
        for y in range(self.vy, min(self.vy + self.vh, mask.height)):
            row = []
            for x in range(self.vx, min(self.vx + self.vw, mask.width)):
                if mask.ignore_mask[y, x]:
                    row.append("~")
                else:
                    cid = int(mask.class_map[y, x])
                    row.append(CATEGORY_GLYPHS[cid % len(CATEGORY_GLYPHS)])
            lines.append("".join(row))
        lines.append("legend: ~=ignore  glyph=category (see `metrics`)")
        return "\n".join(lines)

    def render_metrics(self) -> str:
        report = self.session.report
        lines = [
            f"mIoU={report.miou:.6f}  PQ(mean)={report.pq_mean:.6f}  "
            f"IoU-threshold={self.session.cfg.iou_threshold}",
            "cat | IoU      | PQ       | SQ       | RQ       | TP FP FN",
        ]
        for cid, m in sorted(report.per_category.items()):
            iou = f"{m.iou:.6f}" if m.iou is not None else "  n/a  "
            lines.append(
                f"{cid:>3} | {iou} | {m.pq:.6f} | {m.sq:.6f} | "
                f"{m.rq:.6f} | {m.tp:>2} {m.fp:>2} {m.fn:>2}"
            )
        return "\n".join(lines)

    def render_snapshot(self) -> str:
        snap = self.session.manager.current()
        idx = self.session.index
        return (
            f"snapshot version={snap.version} checksum={snap.checksum[:16]}... "
            f"entries={idx.num_entries} mask={idx.mask.width}x{idx.mask.height} "
            f"index_checksum={idx.checksum()[:16]}..."
        )

    def render_errors(self) -> str:
        if not self.session.recent_errors:
            return "no business errors recorded"
        return "\n".join(
            json.dumps(e, ensure_ascii=False) for e in self.session.recent_errors
        )

    @staticmethod
    def render_query(result) -> str:
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    # ----------------------------------------------------------------- demo
    def run_demo(self) -> str:
        out = ["== demo: scripted interactive run =="]
        mask = self.session.mask
        cx, cy = mask.width // 2, mask.height // 2
        out.append(f"-- query pixel ({cx},{cy})")
        out.append(self.render_query(self.session.query_pixel(cx, cy)))
        first = self.session.index.entries[0].instance_id
        out.append(f"-- query instance {first}")
        out.append(self.render_query(self.session.query_instance(first)))
        out.append(f"-- consistency: {self.session.verify_consistency()}")
        out.append(f"-- self-check: {self.session.self_check()}")
        return "\n".join(out)

    # ----------------------------------------------------------------- repl
    def handle(self, line: str) -> str:
        parts = line.split()
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "view":
                return self.render_view()
            if cmd == "pan":
                dx, dy = int(args[0]), int(args[1])
                self.vx = max(0, min(self.vx + dx, self.session.mask.width - 1))
                self.vy = max(0, min(self.vy + dy, self.session.mask.height - 1))
                return self.render_view()
            if cmd == "query" and args[0] == "pixel":
                return self.render_query(
                    self.session.query_pixel(int(args[1]), int(args[2]))
                )
            if cmd == "query" and args[0] == "instance":
                return self.render_query(self.session.query_instance(int(args[1])))
            if cmd == "metrics":
                return self.render_metrics()
            if cmd == "snapshot":
                return self.render_snapshot()
            if cmd == "errors":
                return self.render_errors()
            if cmd == "selfcheck":
                return json.dumps(self.session.self_check(), indent=2)
            if cmd == "consistency":
                return json.dumps(self.session.verify_consistency(), indent=2)
            if cmd == "demo":
                return self.run_demo()
            if cmd in ("help", "?"):
                return HELP
            if cmd in ("quit", "exit"):
                raise EOFError
            return f"unknown command: {cmd} (try `help`)"
        except PanoError as err:
            return json.dumps(err.to_dict(), ensure_ascii=False, indent=2)
        except (IndexError, ValueError) as exc:
            return f"bad arguments: {exc}"


HELP = """commands:
  view                      render the mask viewport
  pan <dx> <dy>             drag the viewport
  query pixel <x> <y>       interactive per-pixel query
  query instance <id>       interactive per-instance query
  metrics                   mIoU / PQ details per category
  snapshot                  snapshot version + checksums
  errors                    recent business error codes
  consistency               full query-vs-batch verification
  selfcheck                 cross-module self-check
  demo                      run a scripted demonstration
  quit                      exit the workbench
"""


def main():
    cfg = WorkbenchConfig()
    wb = Workbench(cfg)
    print(f"panoptic workbench ready: seed={cfg.seed} "
          f"size={cfg.width}x{cfg.height} budget={cfg.latency_budget_ms}ms")
    print(wb.render_snapshot())
    print(HELP)
    while True:
        try:
            line = input("pano> ")
        except EOFError:
            break
        try:
            out = wb.handle(line)
        except EOFError:
            break
        if out:
            print(out)


if __name__ == "__main__":
    main()
