#!/usr/bin/env python3
"""本地可交互演示入口：逐波段查看、替换重算、快照恢复。

用法: python3 demo.py [--root 工作目录]
命令:
  bands                              列出波段与状态
  show <band> [raw|corr|refl]        查看某波段某层影像（ASCII）与统计
  composite                          查看真彩合成与截断/替换记录
  solve <band>                       查看该波段标定求解与剔除记录
  regions                            查看区域偏差报告
  untrusted                          查看不可信波段及全部原因
  replace-panel <band> <i> <x> <y> <refl>   替换该波段第 i 个定标点
  replace-vignette <band> <c2> <c4>         替换该波段暗角参数
  recompute                          后台增量重算（不阻塞其他命令）
  status                             查看重算状态
  save                               保存快照
  exit                               退出（重算进行中也可退出）
"""
from __future__ import annotations

import argparse
import sys
import threading

import numpy as np

from calib.engine import Engine, SnapshotError

CHARS = " .:-=+*#%@"


def ascii_img(arr, width=64, height=20):
    a = np.asarray(arr, dtype=np.float64)
    a = np.nan_to_num(a, nan=0.0)
    h, w = a.shape
    bh, bw = max(1, h // height), max(1, w // width)
    a = a[:h // bh * bh, :w // bw * bw]
    a = a.reshape(h // bh, bh, w // bw, bw).mean(axis=(1, 3))
    lo, hi = a.min(), a.max()
    norm = (a - lo) / (hi - lo + 1e-12)
    idx = (norm * (len(CHARS) - 1)).astype(int)
    return "\n".join("".join(CHARS[i] for i in row) for row in idx)


def stats_line(arr):
    a = np.asarray(arr, dtype=np.float64)
    return "min=%.4f max=%.4f mean=%.4f" % (
        np.nanmin(a), np.nanmax(a), np.nanmean(a))


class Workbench:
    def __init__(self, root):
        self.engine = Engine(root)
        self.lock = threading.Lock()
        self.worker = None
        self.last_error = None
        snap = self.engine.root / "snapshot" / "manifest.json"
        if snap.exists():
            try:
                self.engine.load_snapshot()
                print("已从最近一次完整快照恢复标定状态。")
                return
            except SnapshotError as exc:
                print("快照不可用（%s），改为全量重算。" % exc)
        self.engine.recompute()
        self.engine.save_snapshot()
        print("已完成初始全量标定并保存快照。")

    def recompute_async(self):
        if self.worker is not None and self.worker.is_alive():
            print("重算已在进行中。")
            return

        def run():
            try:
                info = self.engine.recompute()
                self.engine.save_snapshot()
                with self.lock:
                    self.last_error = None
                print("\n[重算完成] 重算波段=%s 复用缓存=%s" %
                      (info["recomputed"], info["cached"]))
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.last_error = str(exc)
                print("\n[重算失败] %s" % exc)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        print("后台重算已启动（期间可继续输入命令，exit 可随时退出）。")

    def cmd_bands(self):
        res = self.engine.results
        for b in self.engine.project["bands"]:
            flags = ""
            if res and b in res["untrusted"]:
                flags = " 不可信: " + "; ".join(
                    r["reason"] for r in res["untrusted"][b])
            ver = res["versions"][b] if res else self.engine.param_version(b)
            print("  %-6s 版本=%s%s" % (b, ver, flags))

    def cmd_show(self, band, layer="refl"):
        eng = self.engine
        if band not in eng.project["bands"]:
            print("未知波段: %s" % band)
            return
        if eng.results is None:
            print("尚无结果。")
            return
        if layer == "raw":
            arr = eng.project["bands_data"][band]["dn"]
        elif layer == "corr":
            arr = eng.results["bands"][band]["corrected"]
        elif layer == "refl":
            arr = eng.results["aligned"][band]
        else:
            print("未知图层: %s（可选 raw/corr/refl）" % layer)
            return
        print("[%s %s] %s" % (band, layer, stats_line(arr)))
        print(ascii_img(arr))
        if layer == "refl":
            reg = eng.results["registration"][band]
            print("配准: dy=%d dx=%d 超限=%s 对齐残差=%.4f" % (
                reg["dy"], reg["dx"], reg["offset_exceeded"],
                reg["align_residual"]))
            if band in eng.results["untrusted"]:
                for r in eng.results["untrusted"][band]:
                    print("不可信: %s - %s" % (r["reason"], r["detail"]))
            for name, per in eng.results["regions"].items():
                rec = per[band]
                print("区域 %-10s 均值=%.4f 真值=%.4f 偏差=%+.4f" % (
                    name, rec["mean"], rec["truth"], rec["bias"]))

    def cmd_composite(self):
        res = self.engine.results
        if res is None:
            print("尚无结果。")
            return
        rec = res["composite_record"]
        for ch, c in rec["channels"].items():
            note = "（替换自 %s: %s）" % (c["mapped"], c["reason"]) \
                if c["substituted"] else ""
            print("通道 %s 使用波段 %s%s" % (ch, c["used"], note))
        clip = rec["clip"]
        print("截断: [%.1f, %.1f] 截断前 min=%.4f max=%.4f 下截像素=%d 上截像素=%d"
              % (clip["lower"], clip["upper"], clip["pre_min"], clip["pre_max"],
                 clip["clipped_low_pixels"], clip["clipped_high_pixels"]))
        print("[composite] %s" % stats_line(res["composite"]))
        print(ascii_img(res["composite"].mean(axis=-1)))

    def cmd_solve(self, band):
        res = self.engine.results
        if res is None or band not in res["bands"]:
            print("未知波段或尚无结果: %s" % band)
            return
        s = res["bands"][band]["solve"]
        print("[%s] 增益项 a=%.4f 大气项 b=%.4f 自由度=%d 剩余点=%s 剔除点=%s"
              % (band, s["a"], s["b"], s["dof"], s["kept_ids"], s["rejected_ids"]))
        for r in s["rounds"]:
            print("  第%d轮剔除 点%d 坐标=%s 残差=%.2f 阈值=%.2f"
                  % (r["round"], r["removed_id"], r["removed_xy"],
                     r["residual"], r["limit"]))
        for note in s["notes"]:
            print("  记录: %s" % note)
        summ = res["bands"][band]["correction_summary"]
        print("  校正摘要: 暗电流均值=%.2f 暗角范围=[%.3f, %.3f]" % (
            summ["dark_subtracted_mean"], summ["vignette_divided_min"],
            summ["vignette_divided_max"]))

    def cmd_regions(self):
        res = self.engine.results
        if res is None:
            print("尚无结果。")
            return
        for name, per in res["regions"].items():
            print("区域 %s:" % name)
            for b, rec in per.items():
                print("  %-6s 均值=%.4f 真值=%.4f 偏差=%+.4f" % (
                    b, rec["mean"], rec["truth"], rec["bias"]))

    def cmd_untrusted(self):
        res = self.engine.results
        if res is None:
            print("尚无结果。")
            return
        if not res["untrusted"]:
            print("全部波段可信。")
            return
        for b, reasons in res["untrusted"].items():
            print("波段 %s:" % b)
            for r in reasons:
                print("  - %s: %s" % (r["reason"], r["detail"]))

    def cmd_status(self):
        running = self.worker is not None and self.worker.is_alive()
        print("重算状态: %s" % ("进行中" if running else "空闲"))
        if self.engine.last_recompute:
            info = self.engine.last_recompute
            print("最近一次: 重算波段=%s 复用缓存=%s 完成于=%s" % (
                info["recomputed"], info["cached"], info["finished_at"]))
        if self.last_error:
            print("最近错误: %s" % self.last_error)

    def dispatch(self, line):
        parts = line.split()
        if not parts:
            return True
        cmd, args = parts[0], parts[1:]
        if cmd in ("exit", "quit"):
            return False
        if cmd == "help":
            print(__doc__)
        elif cmd == "bands":
            self.cmd_bands()
        elif cmd == "show":
            self.cmd_show(args[0] if args else "",
                          args[1] if len(args) > 1 else "refl")
        elif cmd == "composite":
            self.cmd_composite()
        elif cmd == "solve":
            self.cmd_solve(args[0] if args else "")
        elif cmd == "regions":
            self.cmd_regions()
        elif cmd == "untrusted":
            self.cmd_untrusted()
        elif cmd == "replace-panel" and len(args) == 5:
            self.engine.replace_panel_point(args[0], int(args[1]), int(args[2]),
                                            int(args[3]), float(args[4]))
            print("已替换 %s 第 %s 个定标点，版本=%s（需 recompute 生效）"
                  % (args[0], args[1], self.engine.param_version(args[0])))
        elif cmd == "replace-vignette" and len(args) == 3:
            self.engine.replace_vignette(args[0], float(args[1]), float(args[2]))
            print("已替换 %s 暗角参数，版本=%s（需 recompute 生效）"
                  % (args[0], self.engine.param_version(args[0])))
        elif cmd == "recompute":
            self.recompute_async()
        elif cmd == "status":
            self.cmd_status()
        elif cmd == "save":
            path = self.engine.save_snapshot()
            print("快照已保存: %s" % path)
        else:
            print("未知命令，输入 help 查看用法。")
        return True


def main():
    parser = argparse.ArgumentParser(description="多波段反射率标定演示工作台")
    parser.add_argument("--root", default="workdir", help="工作目录（缓存与快照）")
    args = parser.parse_args()
    wb = Workbench(args.root)
    interactive = sys.stdin.isatty()
    if interactive:
        print("输入 help 查看命令。")
    while True:
        try:
            if interactive:
                line = input("calib> ")
            else:
                line = sys.stdin.readline()
                if line == "":
                    break
        except (EOFError, KeyboardInterrupt):
            break
        if not wb.dispatch(line.strip()):
            break
    print("已退出；最近一次完整快照可用于下次恢复。")


if __name__ == "__main__":
    main()
