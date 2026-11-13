"""自检子命令（任务第 14 条）。

在内置确定性样本上端到端验证：配置校验、可达性失败、注入检测、
正常回显不误报、二次确认、修复对照、残留识别、故障续扫、
报告字节级可复现、工作目录无关性、生成物确定性。

输出各阶段通过数量，全部通过时退出码为 0，否则为 1。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
from pathlib import Path

from . import analysis, client, lab, report as report_mod
from .compare import build_comparison
from .config import ConfigError, ParamSpec, TargetConfig, load_config
from .scanner import BaselineUnreachable, run_scan
from .setup import setup_workspace
from .state import ScanState, target_fingerprint


class CheckFailed(AssertionError):
    pass


class Stage:
    def __init__(self, name: str):
        self.name = name
        self.total = 0
        self.passed = 0
        self.failures: list[str] = []

    def check(self, cond: bool, label: str) -> None:
        self.total += 1
        if cond:
            self.passed += 1
        else:
            self.failures.append(label)
            raise CheckFailed(label)

    def line(self) -> str:
        status = "PASS" if self.passed == self.total else "FAIL"
        return f"[{status}] {self.name}: {self.passed}/{self.total} 通过"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start(mode: str, db_path: Path):
    port = _free_port()
    httpd = lab.make_server("127.0.0.1", port, db_path, mode)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _cfg(port: int, name: str = "selfcheck") -> TargetConfig:
    return TargetConfig(
        target_name=name,
        base_url=f"http://127.0.0.1:{port}",
        endpoint_path="/orders",
        method="GET",
        baseline={"order_no": "ORD-1001", "customer_id": "C001"},
        params=[
            ParamSpec("customer_id", "query", "string", "客户号参数"),
            ParamSpec("order_no", "query", "string", "订单号参数"),
        ],
        data_path=["data", "orders"],
        order_id_field="order_id",
        timeout_seconds=5,
    )


def _scan_once(cfg: TargetConfig, state_path: Path | None = None) -> dict:
    state = ScanState(target_fingerprint(cfg))
    if state_path is not None:
        state = ScanState.load(state_path, target_fingerprint(cfg))
    outcome = run_scan(cfg, state)
    if state_path is not None:
        state.save(state_path)
    return report_mod.build_report(cfg, outcome)


def run_selfcheck() -> tuple[list[Stage], bool]:
    stages: list[Stage] = []
    tmp = Path(tempfile.mkdtemp(prefix="sqli_audit_selfcheck_"))
    db_path = lab.build_sample_db(tmp / "sample_orders.db")

    servers = []
    try:
        # ---- 阶段 1：配置校验 ----
        st = Stage("阶段1 配置严格校验（缺字段/错类型/非回环/校验前不发请求）")
        bad_missing = {
            "base_url": "http://127.0.0.1:8031",
            "endpoint_path": "/orders",
            "method": "GET",
            "baseline": {"order_no": "ORD-1001"},
        }
        p = tmp / "bad_missing.json"
        p.write_text(json.dumps(bad_missing), encoding="utf-8")
        try:
            load_config(p)
            st.check(False, "缺字段配置应当被拒绝")
        except ConfigError as exc:
            joined = " | ".join(exc.problems)
            st.check("target_name" in joined, "错误信息列出缺失字段 target_name")
            st.check("params" in joined, "错误信息列出缺失字段 params")

        bad_type = dict(bad_missing)
        bad_type.update({
            "target_name": "x", "params": [{"name": "order_no", "location": "query"}],
            "timeout_seconds": "five",
        })
        p2 = tmp / "bad_type.json"
        p2.write_text(json.dumps(bad_type), encoding="utf-8")
        try:
            load_config(p2)
            st.check(False, "类型错误配置应当被拒绝")
        except ConfigError as exc:
            st.check(
                any("timeout_seconds" in x for x in exc.problems),
                "错误信息点名 timeout_seconds 类型错误",
            )
            st.check(
                any("params[0].type" in x or "params[0].label" in x for x in exc.problems),
                "错误信息定位到具体参数下标与字段",
            )

        # 校验阶段不得发请求：打桩 client.send，load_config 期间若被调用即失败
        original_send = client.send
        calls = []
        client.send = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
            CheckFailed("配置校验阶段发起了网络请求"))
        try:
            try:
                load_config(p)
            except ConfigError:
                pass
        finally:
            client.send = original_send
        st.check(not calls, "配置非法时无任何请求发出")

        non_loop = {
            "target_name": "x", "base_url": "http://8.8.8.8:80/orders",
            "endpoint_path": "/orders", "method": "GET",
            "baseline": {"order_no": "ORD-1001"},
            "params": [{"name": "order_no", "location": "query", "type": "string", "label": "订单号"}],
        }
        p3 = tmp / "non_loopback.json"
        p3.write_text(json.dumps(non_loop), encoding="utf-8")
        try:
            load_config(p3)
            st.check(False, "非回环目标应当被拒绝")
        except ConfigError as exc:
            st.check(any("回环" in x for x in exc.problems), "非回环地址被明确拒绝")
        stages.append(st)

        # ---- 阶段 2：基线可达性 ----
        st = Stage("阶段2 基线可达性失败即终止")
        dead_port = _free_port()
        try:
            _scan_once(_cfg(dead_port))
            st.check(False, "不可达目标应抛出 BaselineUnreachable")
        except BaselineUnreachable as exc:
            st.check("不可达" in str(exc), "失败原因说明可读")
            st.check(str(dead_port) in str(exc), "异常携带目标端口信息")
        stages.append(st)

        # ---- 启动各模式服务 ----
        srv_v, port_v = _start("vulnerable", db_path)
        srv_f, port_f = _start("fixed", db_path)
        srv_p, port_p = _start("partial", db_path)
        srv_e, port_e = _start("echo", db_path)
        srv_k, port_k = _start("flaky", db_path)
        servers = [srv_v, srv_f, srv_p, srv_e, srv_k]

        # ---- 阶段 3：缺陷接口检测与定位 ----
        st = Stage("阶段3 缺陷接口注入检测、定位与二次确认")
        rep_v = _scan_once(_cfg(port_v, "vuln"))
        pm = {r["name"]: r for r in rep_v["parameters"]}
        st.check(set(pm) == {"order_no", "customer_id"}, "两个参数均被覆盖")
        st.check(pm["order_no"]["status"] == "vulnerable_confirmed", "订单号参数判定为已确认注入")
        st.check(pm["order_no"]["location"] == "query", "可疑点位置准确标注为 query")
        st.check(pm["customer_id"]["status"] == "vulnerable_confirmed", "客户号拼接参数同样确认")
        st.check(pm["order_no"]["confirmation_status"] == "confirmed", "确认状态标注为 confirmed")
        st.check("LEAK" in pm["order_no"]["evidence_summary"] or "布尔" in pm["order_no"]["evidence_summary"],
                 "证据摘要包含具体差异（哨兵行/布尔差异）")
        stages.append(st)

        # ---- 阶段 4：正常回显不误报 ----
        st = Stage("阶段4 仅回显订单号的正常接口零误报")
        rep_e = _scan_once(_cfg(port_e, "echo"))
        st.check(all(not r["suspicious"] for r in rep_e["parameters"]), "回显接口无可疑参数")
        st.check(all(r["status"] == "not_suspicious" for r in rep_e["parameters"]), "状态均为 not_suspicious")
        stages.append(st)

        # ---- 阶段 5：参数化修复版本 ----
        st = Stage("阶段5 参数化版本复测无触发")
        rep_f = _scan_once(_cfg(port_f, "fixed"))
        st.check(all(not r["suspicious"] for r in rep_f["parameters"]), "修复版本无可疑参数")
        st.check(rep_f["scan_summary"]["coverage_complete"] is True, "修复版本扫描覆盖完整")
        stages.append(st)

        # ---- 阶段 6：修复对照与残留识别 ----
        st = Stage("阶段6 修复前后对照与残留项单列")
        state_p = ScanState(target_fingerprint(_cfg(port_p)))
        outcome_p = run_scan(_cfg(port_p), state_p)
        cmp_rep = build_comparison(_cfg(port_p, "partial"), rep_v, outcome_p)
        cm = {c["name"]: c for c in cmp_rep["comparisons"]}
        st.check(cm["customer_id"]["conclusion"] == "fixed", "客户号对照结论为 fixed")
        st.check(cm["order_no"]["conclusion"] == "remains_vulnerable", "订单号对照结论为 remains_vulnerable")
        residual = cmp_rep["residual_vulnerable_params"]
        st.check([r["name"] for r in residual] == ["order_no"], "残留清单仅列出未修复的 order_no")
        st.check(all(k in residual[0] for k in ("after_status", "evidence_summary", "suspicious_probe_ids")),
                 "残留项携带状态与证据字段，对照字段完整")
        stages.append(st)

        # ---- 阶段 7：故障不中断 + 恢复续扫 ----
        st = Stage("阶段7 单探测失败不中断、恢复后续扫且不重复计数")
        state_file = tmp / "resume_state.json"
        os.environ["SQLI_AUDIT_FAIL_AFTER"] = "5"
        try:
            cfg_k = _cfg(port_k, "flaky")
            state1 = ScanState.load(state_file, target_fingerprint(cfg_k))
            outcome1 = run_scan(cfg_k, state1)
            state1.save(state_file)
            rep1 = report_mod.build_report(cfg_k, outcome1)
        finally:
            del os.environ["SQLI_AUDIT_FAIL_AFTER"]
        st.check(rep1["scan_summary"]["probes_incomplete"] >= 1, "故障期间存在未完成探测项")
        st.check(rep1["scan_summary"]["probes_total"] == 8, "探测总数固定为 参数数×载荷数=8")
        st.check(any(r["status"] in ("incomplete", "suspicious_unconfirmed") for r in rep1["parameters"]),
                 "报告标注未完成/待确认参数")
        cached_customer_p01 = next(
            r for r in next(x for x in rep1["parameters"] if x["name"] == "customer_id")["probe_results"]
            if r["payload_id"] == "P01_TAUTOLOGY"
        )
        st.check(cached_customer_p01["request_outcome"] == "completed", "故障前已完成项保留结论")

        # 恢复后用同一状态文件重跑
        cfg_k2 = _cfg(port_k, "flaky")
        state2 = ScanState.load(state_file, target_fingerprint(cfg_k2))
        outcome2 = run_scan(cfg_k2, state2)
        rep2 = report_mod.build_report(cfg_k2, outcome2)
        st.check(rep2["scan_summary"]["coverage_complete"] is True, "恢复后未完成项补齐结论")
        st.check(rep2["scan_summary"]["probes_total"] == 8, "重跑不重复计数，探测总数仍为 8")
        kept = next(
            r for r in next(x for x in rep2["parameters"] if x["name"] == "customer_id")["probe_results"]
            if r["payload_id"] == "P01_TAUTOLOGY"
        )
        st.check(kept["response"] == cached_customer_p01["response"], "已完成项响应结论保持不变")
        st.check(all(r["status"] == "vulnerable_confirmed" for r in rep2["parameters"]),
                 "补齐后两个参数均得到已确认结论")
        # 与一次性完整扫描的报告逐字节一致（对齐 target_name 与临时端口后比较主体）
        rep_v2 = json.loads(json.dumps(rep_v))
        aligned_name = rep2["target"]["target_name"]
        rep_v2["target"]["target_name"] = aligned_name
        rep_v2["target"]["base_url"] = rep2["target"]["base_url"]
        rep_v2["business_summary"] = rep_v2["business_summary"].replace("【vuln】", f"【{aligned_name}】")
        rep2_cmp = json.loads(json.dumps(rep2))
        st.check(
            report_mod.canonical_json(rep2_cmp) == report_mod.canonical_json(rep_v2),
            "续扫补齐后的报告与一次性完整扫描逐字节一致（端口/名称对齐后）",
        )
        stages.append(st)

        # ---- 阶段 8：报告可复现性 ----
        st = Stage("阶段8 报告字节级可复现与时间占位")
        rep_a = _scan_once(_cfg(port_v, "rep_a"))
        rep_b = _scan_once(_cfg(port_v, "rep_a"))
        st.check(report_mod.canonical_json(rep_a) == report_mod.canonical_json(rep_b),
                 "同一输入两次运行报告逐字节一致")
        st.check(rep_a["generated_at"] == report_mod.TIME_PLACEHOLDER, "时间字段使用固定占位值")
        st.check(rep_a["elapsed"] == report_mod.TIME_PLACEHOLDER, "耗时字段使用固定占位值")
        # 不同工作目录
        cwd1 = tmp / "cwd_a"
        cwd2 = tmp / "cwd_b"
        cwd1.mkdir()
        cwd2.mkdir()
        old_cwd = Path.cwd()
        os.chdir(cwd1)
        try:
            r1 = report_mod.canonical_json(_scan_once(_cfg(port_v, "cwd_test")))
        finally:
            os.chdir(cwd2)
        try:
            r2 = report_mod.canonical_json(_scan_once(_cfg(port_v, "cwd_test")))
        finally:
            os.chdir(old_cwd)
        st.check(r1 == r2, "不同工作目录下报告主体逐项一致")
        stages.append(st)

        # ---- 阶段 9：业务摘要 ----
        st = Stage("阶段9 业务摘要：覆盖不全不宣称安全")
        st.check("覆盖不全" in rep1["business_summary"], "未完成时摘要明确提示覆盖不全")
        st.check("未覆盖不等于安全" in rep1["business_summary"], "摘要明示未覆盖不等于安全")
        st.check("全部参数无风险" not in rep_a["business_summary"], "完整扫描也不给出绝对化安全承诺")
        st.check("可用结论" in rep_a["business_summary"], "完整扫描提供可用结论")
        stages.append(st)

        # ---- 阶段 10：生成物确定性 ----
        st = Stage("阶段10 setup 生成物确定性与独立脚本可编译")
        ws1 = tmp / "ws1"
        ws2 = tmp / "ws2"
        files1 = setup_workspace(ws1)
        files2 = setup_workspace(ws2)
        st.check([p.name for p in files1] == [p.name for p in files2], "两次生成文件清单一致")

        def digest(ws: Path) -> dict[str, str]:
            out = {}
            for f in sorted(ws.iterdir()):
                if f.is_file():
                    out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
            return out

        st.check(digest(ws1) == digest(ws2), "同名文件内容逐字节一致（样本库/脚本/配置）")
        import py_compile

        for mode in lab.MODES:
            py_compile.compile(str(ws1 / f"server_{mode}.py"), doraise=True)
        st.check(True, "5 个独立接口脚本均可编译")
        stages.append(st)

    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()

    all_ok = all(s.passed == s.total for s in stages)
    return stages, all_ok


def main() -> int:
    stages, ok = run_selfcheck()
    total = passed = 0
    for st in stages:
        print(st.line())
        for f in st.failures:
            print(f"    失败项: {f}")
        total += st.total
        passed += st.passed
    print(f"自检合计: {passed}/{total} 通过")
    return 0 if ok else 1
