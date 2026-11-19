"""交互式诊断工作台。

交互模式:  python -m rope_workbench.workbench interactive
命令模式:  python -m rope_workbench.workbench <cmd> [args]
  cases / run / run-all / angles / heatmap / injections / degradations
  spectrum / resources / errors / verify / rerun
"""

from __future__ import annotations

import cmd
import json
import sys

import numpy as np

from . import report as report_mod
from . import samples, variants, verify
from .config import DEFAULT_CONFIG
from .pipeline import run_case

_RAMP = " .:-=+*#%@"


def ascii_matrix(mat, width=64):
    """把二维矩阵渲染为 ASCII 热图。"""
    mat = np.asarray(mat, dtype=np.float64)
    finite = np.isfinite(mat)
    lo = float(mat[finite].min()) if finite.any() else 0.0
    hi = float(mat[finite].max()) if finite.any() else 1.0
    span = (hi - lo) or 1.0
    rows = []
    step = max(1, mat.shape[1] // width)
    for row in mat:
        cells = []
        for x in row[::step]:
            if not np.isfinite(x):
                cells.append("X")
            else:
                cells.append(_RAMP[min(len(_RAMP) - 1,
                                       int((x - lo) / span * (len(_RAMP) - 1)))])
        rows.append("".join(cells))
    return "\n".join(rows)


def attention_weights(result, head=0):
    """由分块打分重算指定 head 的注意力权重矩阵（用于热图展示）。"""
    from . import attention
    case = result.case
    s = int(case["seq_len"])
    bs = min(int(case["block_size"]), s)
    causal = bool(case["causal"])
    blocks = attention.score_blocks(result.q_rot, result.k_rot, bs, causal)
    w = np.full((s, s), np.nan)
    for (q_lo, q_hi, k_lo, k_hi, sc) in blocks:
        blk = sc[head]
        m = np.where(np.isfinite(blk), blk, -np.inf).max(axis=1, keepdims=True)
        m = np.where(np.isfinite(m), m, 0.0)
        p = np.exp(blk - m)
        denom = p.sum(axis=1, keepdims=True)
        w[q_lo:q_hi, k_lo:k_hi] = p / np.maximum(denom, 1e-300)
    return w


class Workbench:
    """工作台内核：加载清单、运行、缓存结果。"""

    def __init__(self, data_dir="data", cache_dir="cache", config=None):
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.config = config or DEFAULT_CONFIG
        self.cases = []
        self.results = {}
        self.variant_reports = []
        self.verify_issues = []

    def load(self):
        import os
        manifest = os.path.join(self.data_dir, "cases.jsonl")
        if not os.path.exists(manifest):
            samples.generate_all(self.data_dir, self.cache_dir)
        self.cases = samples.read_manifest(manifest)
        return self.cases

    def get_case(self, case_id):
        for c in self.cases:
            if c["case_id"] == case_id:
                return c
        raise KeyError(f"未知 case_id: {case_id}")

    def run(self, case_id):
        case = self.get_case(case_id)
        arrays = samples.load_arrays(case, self.cache_dir)
        r = run_case(case, arrays=arrays, config=self.config)
        self.results[case_id] = r
        return r

    def run_all(self):
        self.load()
        for c in self.cases:
            self.run(c["case_id"])
        self.variant_reports = []
        if "c001_classic" in self.results:
            base = self.results["c001_classic"]
            for cid in ("c002_interp", "c003_ntk", "c004_blockwise",
                        "c009_equiv_interp_s1"):
                if cid in self.results:
                    self.variant_reports.append(
                        variants.adjudicate(base, self.results[cid], self.config))
        self.verify_issues = []
        for c in self.cases:
            r = self.results[c["case_id"]]
            if r.status in ("ok", "degraded"):
                self.verify_issues.extend(verify.verify_case(c, r, self.config))
        return report_mod.run_report(
            [self.results[c["case_id"]] for c in self.cases],
            self.variant_reports, self.verify_issues)


def _fmt_record(rec):
    return json.dumps(rec, ensure_ascii=False, sort_keys=True)


class WorkbenchShell(cmd.Cmd):
    intro = "RoPE 诊断工作台。输入 help 查看命令。"
    prompt = "rope> "

    def __init__(self, wb):
        super().__init__()
        self.wb = wb
        self.wb.load()

    def _result(self, case_id):
        if case_id not in self.wb.results:
            return self.wb.run(case_id)
        return self.wb.results[case_id]

    def do_cases(self, arg):
        "列出任务清单"
        for c in self.wb.cases:
            print(f"{c['case_id']:24s} {c['pos_encoding_variant']:22s} "
                  f"S={c['seq_len']} d={c['head_dim']} B={c['block_size']} "
                  f"s={c['pos_scale']} causal={c['causal']}")

    def do_run(self, arg):
        "run <case_id> 运行单 case"
        r = self._result(arg.strip())
        print(f"status={r.status} max_block_dev={r.max_block_dev:.3e} "
              f"hash={report_mod.result_hash(r)[:16]}...")
        for e in r.errors:
            print("  ERR", _fmt_record(e))

    def do_angles(self, arg):
        "angles <case_id> 旋转角分布"
        r = self._result(arg.strip())
        print(f"角表 {r.angles.shape} "
              f"max|angle|={r.angle_diag['max_abs_angle']:.4e} "
              f"rel_err={r.angle_diag['angle_rel_err']:.3e} "
              f"guard={r.angle_diag['subnormal_guard_triggered']}")
        print(ascii_matrix(r.angles))

    def do_heatmap(self, arg):
        "heatmap <case_id> 分块注意力热图 (head 0)"
        r = self._result(arg.strip())
        print(ascii_matrix(attention_weights(r)))

    def do_injections(self, arg):
        "四阶段注入记录"
        for cid, r in sorted(self.wb.results.items()):
            for inj in r.injections:
                print(_fmt_record(inj))

    def do_degradations(self, arg):
        "降级链路"
        for cid, r in sorted(self.wb.results.items()):
            for d in r.degradations:
                print(_fmt_record(d))

    def do_spectrum(self, arg):
        "spectrum <id_a> <id_b> 跨变体偏差谱"
        a, b = arg.split()[:2]
        rep = variants.adjudicate(self._result(a), self._result(b), self.wb.config)
        print(f"映射: {rep['mapping']}")
        print(f"角度偏差谱 per_dim={rep['angle_dev_spectrum']['per_dim']}")
        print(f"角度最大偏差={rep['angle_dev_spectrum']['max_abs']:.3e}")
        if rep.get("output_dev_spectrum"):
            print(f"输出最大偏差={rep['output_dev_spectrum']['max_abs']:.3e}")
        for e in rep["errors"]:
            print("  ERR", _fmt_record(e))

    def do_resources(self, arg):
        "资源消耗"
        for cid, r in sorted(self.wb.results.items()):
            print(cid, _fmt_record(r.resources))

    def do_errors(self, arg):
        "最近业务错误码"
        for cid, r in sorted(self.wb.results.items()):
            for e in r.errors + r.unattributed:
                print(_fmt_record(e))

    def do_verify(self, arg):
        "verify <case_id> 独立验证器复核"
        case = self.wb.get_case(arg.strip())
        r = self._result(arg.strip())
        issues = verify.verify_case(case, r, self.wb.config)
        print("PASS" if not issues else "FAIL")
        for i in issues:
            print("  ", _fmt_record(i))

    def do_rerun(self, arg):
        "rerun <case_id> 固定种子重跑并比对哈希"
        case = self.wb.get_case(arg.strip())
        arrays = samples.load_arrays(case, self.wb.cache_dir)
        h1 = report_mod.result_hash(run_case(case, arrays=arrays,
                                             config=self.wb.config))
        h2 = report_mod.result_hash(run_case(case, arrays=arrays,
                                             config=self.wb.config))
        print(f"hash1={h1}")
        print(f"hash2={h2}")
        print("MATCH" if h1 == h2 else "MISMATCH")

    def do_run_all(self, arg):
        "运行全部 case + 裁决 + 验证"
        rep = self.wb.run_all()
        print(json.dumps(rep, ensure_ascii=False, indent=1))

    def do_quit(self, arg):
        return True

    do_EOF = do_quit


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    wb = Workbench()
    if not argv or argv[0] == "interactive":
        WorkbenchShell(wb).cmdloop()
        return 0
    command, rest = argv[0], argv[1:]
    shell = WorkbenchShell(wb)
    shell.onecmd(" ".join([command.replace("-", "_")] + rest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
