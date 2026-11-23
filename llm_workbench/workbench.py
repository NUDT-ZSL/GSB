"""Local interactive diagnostics workbench.

REPL commands:
  run                 execute the pipeline (admit, execute, snapshot, selfcheck)
  requests            list requests with sampling params and status
  show <request_id>   per-step candidate truncation detail and draws
  ledger              budget ledger remainders
  decisions           scheduling decision basis, downgrades, arbitration
  recovery            checkpoint / recovery report
  errors              recent business error codes
  selfcheck           show the independent cross-module consistency check
  report [path]       write the full diagnostic report JSON
  quit

Non-interactive: python -m llm_workbench.workbench --data data --work work
--report artifacts/diagnostic_report.json
"""

from __future__ import annotations

import argparse
import cmd
import json
import os

from .pipeline import run_pipeline


def _fmt_params(p: dict) -> str:
    return f"T={p['temperature']} top_k={p['top_k']} top_p={p['top_p']}"


class Workbench(cmd.Cmd):
    intro = "LLM sampling diagnostics workbench. Type 'help' for commands."
    prompt = "wb> "

    def __init__(self, data_dir: str, work_dir: str, use_pool: bool = True):
        super().__init__()
        self.data_dir = data_dir
        self.work_dir = work_dir
        self.use_pool = use_pool
        self.report: dict | None = None

    def _need_report(self) -> dict | None:
        if self.report is None:
            print("no run yet - executing pipeline first...")
            self.do_run("")
        return self.report

    def do_run(self, arg):
        """run: execute the full pipeline."""
        self.report = run_pipeline(self.data_dir, self.work_dir, use_pool=self.use_pool)
        run = self.report.get("run", {})
        rec = run.get("recovery", {})
        print(f"ok={self.report['ok']} admitted={len(self.report.get('results', {}))} "
              f"rejected={len(self.report.get('admission', {}).get('rejected', []))} "
              f"resumed_from={rec.get('resumed_from_index')} "
              f"selfcheck={self.report.get('selfcheck', {}).get('ok')}")

    def do_requests(self, arg):
        """requests: list requests with sampling params and status."""
        rep = self._need_report()
        if not rep:
            return
        results = rep.get("results", {})
        rejected = {r["request_id"]: r["code"]
                    for r in rep.get("admission", {}).get("rejected", [])}
        downgraded = {}
        for d in rep.get("admission", {}).get("downgraded", []):
            downgraded.setdefault(d["request_id"], []).append(d["category"])
        for rid in sorted(set(results) | set(rejected)):
            if rid in results:
                r = results[rid]
                tags = ",".join(downgraded.get(rid, [])) or "ok"
                print(f"{rid}  ADMITTED[{tags}]  len={r['effective_target_len']}  "
                      f"{_fmt_params(r['effective_params'])}")
            else:
                print(f"{rid}  REJECTED[{rejected[rid]}]")

    def do_show(self, arg):
        """show <request_id>: per-step candidate truncation and draws."""
        rep = self._need_report()
        rid = arg.strip()
        if not rep or rid not in rep.get("results", {}):
            print(f"no sampling summary for {rid!r} (rejected requests have none)")
            return
        r = rep["results"][rid]
        print(f"{rid}: {_fmt_params(r['effective_params'])} "
              f"len={r['effective_target_len']}")
        for s in r["steps"]:
            cands = " ".join(f"tok_{t}:{p:.4f}" for t, p in s["candidates"])
            print(f"  step {s['step']:>3}  draw={s['draw']:.6f}  "
                  f"cut={s['truncated_out']:>3}  chose=tok_{s['chosen_token']}  "
                  f"candidates=[{cands}]")

    def do_ledger(self, arg):
        """ledger: budget ledger remainders."""
        rep = self._need_report()
        if rep:
            print(json.dumps(rep.get("admission", {}).get("ledger", {}),
                             indent=2, ensure_ascii=False))
            print(json.dumps(rep.get("run", {}).get("budgets_consumed", {}),
                             indent=2, ensure_ascii=False))

    def do_decisions(self, arg):
        """decisions: scheduling decision basis and downgrades."""
        rep = self._need_report()
        if not rep:
            return
        adm = rep.get("admission", {})
        for rec in adm.get("arbitration", []):
            print(f"[{rec['category']}] {rec['request_id']}: {rec['detail']}")
            print(f"    basis: {rec['basis']}")
        for rec in adm.get("rejected", []):
            print(f"[{rec['code']}] {rec['request_id']}: {rec['reason']}")
            print(f"    remainders: {rec['budget_remainders']}")

    def do_recovery(self, arg):
        """recovery: checkpoint / recovery report."""
        rep = self._need_report()
        if rep:
            print(json.dumps(rep.get("run", {}).get("recovery", {}),
                             indent=2, ensure_ascii=False))

    def do_errors(self, arg):
        """errors: recent business error codes."""
        rep = self._need_report()
        if not rep:
            return
        for e in rep.get("errors", []):
            print(f"{e['code']}  stage={e['context'].get('stage')}  "
                  f"field={e['context'].get('field')}  "
                  f"request_id={e['context'].get('request_id')}: {e['message']}")
        for e in rep.get("selfcheck", {}).get("errors", []):
            print(f"{e['code']}  stage={e['stage']}: {e['message']}")

    def do_selfcheck(self, arg):
        """selfcheck: show the independent consistency check outcome."""
        rep = self._need_report()
        if rep:
            print(json.dumps(rep.get("selfcheck", {}), indent=2, ensure_ascii=False))

    def do_report(self, arg):
        """report [path]: write the full diagnostic report JSON."""
        rep = self._need_report()
        if not rep:
            return
        path = arg.strip() or os.path.join(self.work_dir, "diagnostic_report.json")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print(f"report written to {path}")

    def do_quit(self, arg):
        """quit."""
        return True

    do_EOF = do_quit


def main(argv=None):
    ap = argparse.ArgumentParser(description="local LLM sampling diagnostics workbench")
    ap.add_argument("--data", default="data")
    ap.add_argument("--work", default="work")
    ap.add_argument("--report", help="write diagnostic report and exit")
    ap.add_argument("--no-pool", action="store_true", help="disable the process pool")
    ap.add_argument("--crash-after", type=int, default=None,
                    help="simulate a forced termination after N committed requests")
    args = ap.parse_args(argv)

    if args.report:
        rep = run_pipeline(args.data, args.work, crash_after=args.crash_after,
                           use_pool=not args.no_pool)
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print(json.dumps({
            "ok": rep["ok"],
            "admitted": len(rep.get("results", {})),
            "rejected": len(rep.get("admission", {}).get("rejected", [])),
            "downgraded": len(rep.get("admission", {}).get("downgraded", [])),
            "selfcheck": rep.get("selfcheck", {}).get("ok"),
            "report": args.report,
        }, ensure_ascii=False))
        return 0 if rep["ok"] else 1

    Workbench(args.data, args.work, use_pool=not args.no_pool).cmdloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
