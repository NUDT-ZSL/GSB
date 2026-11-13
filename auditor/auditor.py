# -*- coding: utf-8 -*-
"""
上传服务安全审计工具（仅限授权范围内的本机 127.0.0.1 目标使用）。

能力：
  1. 发送含路径遍历序列（../、..\\、绝对路径）的文件名，判定是否发生上传目录越界写入，
     并在磁盘上核实文件真实落点与内容，报告受影响的绝对路径。
  2. 发送扩展名与真实内容不符的文件，判定服务是否只按扩展名放行（类型绕过），
     列出被错误接受的文件名。
  3. 输出 UTF-8 编码、字段齐全的结构化 JSON 报告，按风险等级（高>中>低）降序排列。
  4. 每个漏洞附“路径规范化”“内容类型校验”两类可操作修复要点。
  5. 沙箱约束：所有探测写入只能落在 --dir 指定的上传目录或其父级测试沙箱内；
     运行开始清理历史探测残留，运行结束清理本次上传目录内的全部测试文件；
     上传目录之外仅保留（可选删除的）漏洞证据文件。
  6. 服务不可达/响应异常：打印明确中文错误、以非零退出码结束，且不生成报告。

仅使用 Python 标准库，Python 3.10+ 可运行。建议以隔离模式执行：python -I auditor.py ...
"""
import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime

# Windows 控制台默认可能是 GBK 代码页，统一切到 UTF-8，避免中文输出乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------- 常量定义
PROBE_PREFIX = "auditscan_"
RISK_ORDER = {"高": 3, "中": 2, "低": 1}
# 一个最小的 PNG 文件头（真实图片以该魔数开头），用于“合法内容”基线与遍历探测
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
JPG_SIGNATURE = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
PDF_SIGNATURE = b"%PDF-1.7\n"
TEXT_MARKER = b"#!/bin/sh\necho 'this is not an image'\n"


def now_iso() -> str:
    """带本地时区偏移的 ISO 8601 时间戳。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP 与 multipart
def build_multipart(filename: str, content: bytes, boundary: str, field: str = "file") -> bytes:
    """手工构造 multipart/form-data 请求体，避免依赖第三方库。"""
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + content + tail


def http_request(method: str, url: str, timeout: float, body=None, headers=None):
    """发起 HTTP 请求；连接类错误向上抛出 URLError，HTTP 4xx/5xx 正常返回状态码。"""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------- 路径工具
def is_within(path: str, root: str) -> bool:
    """判断规范化后的 path 是否位于 root 之内（兼容 Windows 盘符/反斜杠）。"""
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) == os.path.realpath(root)
    except ValueError:
        return False


def expected_landing(upload_dir: str, submitted_filename: str) -> str:
    """按服务端 os.path.join(UPLOAD_DIR, filename) 的方式预测文件落点（绝对路径）。"""
    return os.path.abspath(os.path.join(upload_dir, submitted_filename))


# ---------------------------------------------------------------- 沙箱与清理
def remove_probe_files(root: str, recursive: bool, token: str = None):
    """删除 root 下由审计工具产生的探测文件，返回已删除的绝对路径列表。"""
    removed = []
    if not os.path.isdir(root):
        return removed
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.startswith(PROBE_PREFIX) and (token is None or token in name):
                    victim = os.path.join(dirpath, name)
                    try:
                        os.remove(victim)
                        removed.append(os.path.abspath(victim))
                    except OSError:
                        pass
        # 清理因遍历尝试产生的空目录（自底向上，保留根目录）
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
    else:
        for name in os.listdir(root):
            candidate = os.path.join(root, name)
            if os.path.isfile(candidate) and name.startswith(PROBE_PREFIX) and (
                token is None or token in name
            ):
                try:
                    os.remove(candidate)
                    removed.append(os.path.abspath(candidate))
                except OSError:
                    pass
    return removed


def scan_probe_files(root: str, token: str):
    """枚举 root 下仍存在的本次探测文件（用于磁盘核实与报告）。"""
    found = []
    if not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith(PROBE_PREFIX) and token in name:
                found.append(os.path.abspath(os.path.join(dirpath, name)))
    return found


# ---------------------------------------------------------------- 审计主流程
class Auditor:
    def __init__(self, args):
        self.base_url = args.url.rstrip("/")
        self.upload_dir = os.path.abspath(args.dir)
        self.sandbox_root = os.path.abspath(args.sandbox_root or os.path.dirname(self.upload_dir))
        self.report_path = os.path.abspath(args.report)
        self.timeout = args.timeout
        self.clean_evidence = args.clean_evidence
        self.token = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(4)
        self.boundary = "----auditorboundary" + secrets.token_hex(8)
        self.findings = []
        self.checks = []
        self.warnings = []
        self.removed_at_start = []
        self.removed_at_end = []
        self.retained_evidence = []

    # ---------- 通用上传
    def upload(self, filename: str, content: bytes):
        body = build_multipart(filename, content, self.boundary)
        headers = {"Content-Type": f"multipart/form-data; boundary={self.boundary}"}
        status, raw = http_request(
            "POST", self.base_url + "/upload", self.timeout, body=body, headers=headers
        )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw_response": raw[:200].decode("utf-8", "replace")}
        return status, payload

    # ---------- 0. 连通性检查（失败即中文报错、非零退出、不产出报告）
    def preflight(self):
        try:
            status, raw = http_request("GET", self.base_url + "/health", self.timeout)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            print(
                f"错误：无法连接上传服务 {self.base_url}（{reason}）。\n"
                "请确认服务已在本机 127.0.0.1 启动且端口正确，然后重新运行本工具。",
                file=sys.stderr,
            )
            return 1
        except (TimeoutError, OSError) as exc:
            print(f"错误：连接上传服务 {self.base_url} 时发生网络异常：{exc}", file=sys.stderr)
            return 1
        if status != 200:
            print(
                f"错误：目标服务响应异常，/health 返回 HTTP {status}，无法确认服务身份，已终止扫描（未生成报告）。",
                file=sys.stderr,
            )
            return 2
        try:
            info = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            print("错误：目标服务 /health 返回内容不是合法 JSON，已终止扫描（未生成报告）。", file=sys.stderr)
            return 2
        server_dir = info.get("upload_dir")
        if server_dir and os.path.abspath(server_dir).lower() != self.upload_dir.lower():
            self.warnings.append(
                f"服务自报上传目录为 {server_dir}，与 --dir 指定的 {self.upload_dir} 不一致，"
                "越界判定以 --dir 指定目录为准，请确认参数是否正确。"
            )
        print(f"[1/5] 连通性检查通过：服务={info.get('service', 'unknown')}，HTTP {status}")
        return 0

    # ---------- 1. 基线：合法 PNG 必须被接受，否则服务行为与预期不符，不产出误导性报告
    def baseline(self):
        baseline_name = f"{PROBE_PREFIX}{self.token}_baseline.png"
        content = PNG_SIGNATURE + f"|{self.token}|baseline".encode()
        status, payload = self.upload(baseline_name, content)
        ok = status == 200 and os.path.isfile(os.path.join(self.upload_dir, baseline_name))
        self.checks.append(
            {
                "check": "合法 PNG 基线上传",
                "submitted_filename": baseline_name,
                "http_status": status,
                "result": "通过" if ok else "失败",
            }
        )
        if not ok:
            print(
                "错误：合法 PNG 基线文件上传被拒绝（HTTP %s），目标服务行为与预期不符，"
                "继续扫描将产生误导性结论，已终止（未生成报告）。" % status,
                file=sys.stderr,
            )
            return 2
        print(f"[2/5] 基线上传通过：{baseline_name}（HTTP {status}）")
        return 0

    # ---------- 2. 路径遍历写入探测
    def test_path_traversal(self):
        print("[3/5] 路径遍历写入探测 ...")
        payloads = [
            ("Unix 风格 ../", f"../{PROBE_PREFIX}{self.token}_traversal_unix.png"),
            ("Windows 反斜杠 ..\\", f"..\\{PROBE_PREFIX}{self.token}_traversal_win.png"),
            ("绝对路径/盘符", os.path.join(self.sandbox_root, f"{PROBE_PREFIX}{self.token}_traversal_abs.png")),
        ]
        evidence = []
        affected = []
        for label, filename in payloads:
            landing = expected_landing(self.upload_dir, filename)
            # 沙箱强制约束：任何预期落点逃出测试沙箱，拒绝发送该探测
            if not is_within(landing, self.sandbox_root):
                raise RuntimeError(f"内部安全约束失败：探测落点 {landing} 超出测试沙箱 {self.sandbox_root}")
            content = PNG_SIGNATURE + f"|{self.token}|traversal|{label}".encode()
            status, payload = self.upload(filename, content)
            verified = os.path.isfile(landing) and self._file_contains(landing, self.token)
            outside = verified and not is_within(landing, self.upload_dir)
            evidence.append(
                {
                    "technique": label,
                    "submitted_filename": filename,
                    "expected_landing_path": landing,
                    "http_status": status,
                    "server_response": payload,
                    "file_verified_on_disk": verified,
                    "written_outside_upload_dir": outside,
                }
            )
            print(
                f"    - {label}: HTTP {status}，磁盘核实={'是' if verified else '否'}，"
                f"越界={'是' if outside else '否'}"
            )
            if outside:
                affected.append(landing)
                self.retained_evidence.append(landing)
            self.checks.append(
                {
                    "check": f"路径遍历-{label}",
                    "submitted_filename": filename,
                    "http_status": status,
                    "result": "存在越界写入" if outside else "已拦截/未越界",
                }
            )

        # 兜底：扫描沙箱内是否出现任何预期之外的本次探测文件
        for extra in scan_probe_files(self.sandbox_root, self.token):
            if not is_within(extra, self.upload_dir) and extra not in affected:
                affected.append(extra)
                self.retained_evidence.append(extra)
                evidence.append(
                    {
                        "technique": "沙箱兜底扫描发现的非预期落点",
                        "submitted_filename": None,
                        "expected_landing_path": extra,
                        "http_status": None,
                        "server_response": None,
                        "file_verified_on_disk": True,
                        "written_outside_upload_dir": True,
                    }
                )

        if affected:
            self.findings.append(
                {
                    "id": "PATH_TRAVERSAL_WRITE",
                    "vulnerability_type": "路径遍历写入",
                    "risk_level": "高",
                    "title": "服务未规范化文件名路径，攻击者可将文件写入上传目录之外",
                    "status": "confirmed",
                    "affected_paths": sorted(set(affected)),
                    "evidence": evidence,
                    "detected_at": now_iso(),
                    "remediation": {
                        "path_normalization": (
                            "1) 先用 os.path.basename 剥离文件名中的任何目录成分（同时处理 / 与 \\）；"
                            "2) 对上传目录与目标路径分别执行 os.path.realpath 规范化；"
                            "3) 用 os.path.commonpath 校验目标路径必须等于或位于上传目录之内，"
                            "不在边界内则返回 400 拒绝写入；4) 禁止直接 os.path.join 客户端原始 filename。"
                        ),
                        "content_type_validation": (
                            "路径修复之外仍需做内容校验：结合扩展名白名单与魔数（magic bytes）"
                            "双重验证文件真实类型，防止越界写入的内容被当作可信资源解析执行。"
                        ),
                    },
                }
            )

    @staticmethod
    def _file_contains(path: str, marker: str) -> bool:
        try:
            with open(path, "rb") as fh:
                return marker.encode("ascii") in fh.read()
        except OSError:
            return False

    # ---------- 3. 扩展名/内容不一致的类型绕过探测
    def test_type_bypass(self):
        print("[4/5] 扩展名与内容不一致的类型绕过探测 ...")
        probes = [
            (f"{PROBE_PREFIX}{self.token}_bypass_png.png", TEXT_MARKER + b"<?php phpinfo(); ?>\n", "PHP 脚本内容伪装为 PNG"),
            (f"{PROBE_PREFIX}{self.token}_bypass_jpg.jpg", b"MZ\x90\x00 this is a PE executable placeholder\n", "可执行程序内容伪装为 JPG"),
            (f"{PROBE_PREFIX}{self.token}_bypass_pdf.pdf", b"plain text, definitely not a pdf document\n", "纯文本伪装为 PDF"),
        ]
        evidence = []
        accepted = []
        for filename, content, label in probes:
            status, payload = self.upload(filename, content)
            landing = os.path.join(self.upload_dir, filename)
            on_disk = os.path.isfile(landing)
            bypassed = status == 200 and on_disk
            evidence.append(
                {
                    "technique": label,
                    "submitted_filename": filename,
                    "fake_extension": os.path.splitext(filename)[1],
                    "content_preview": content[:48].decode("utf-8", "replace"),
                    "http_status": status,
                    "server_response": payload,
                    "file_verified_on_disk": on_disk,
                    "wrongly_accepted": bypassed,
                }
            )
            print(f"    - {label}（{filename}）: HTTP {status}，错误放行={'是' if bypassed else '否'}")
            if bypassed:
                accepted.append(filename)
            self.checks.append(
                {
                    "check": f"类型绕过-{label}",
                    "submitted_filename": filename,
                    "http_status": status,
                    "result": "被错误接受" if bypassed else "已拒绝",
                }
            )
        if accepted:
            self.findings.append(
                {
                    "id": "CONTENT_TYPE_BYPASS",
                    "vulnerability_type": "扩展名/内容不一致的类型绕过",
                    "risk_level": "中",
                    "title": "服务仅按扩展名将文件放行，未校验真实内容类型",
                    "status": "confirmed",
                    "affected_paths": [os.path.abspath(os.path.join(self.upload_dir, name)) for name in accepted],
                    "wrongly_accepted_filenames": accepted,
                    "evidence": evidence,
                    "detected_at": now_iso(),
                    "remediation": {
                        "path_normalization": (
                            "先完成文件名 basename 剥离与 realpath/commonpath 目录边界校验，"
                            "确保通过内容校验的文件只能写入上传目录内部。"
                        ),
                        "content_type_validation": (
                            "1) 扩展名白名单仅作为第一层；2) 读取文件头魔数判断真实类型"
                            "（PNG: 89 50 4E 47、JPG: FF D8 FF、GIF: GIF87a/GIF89a、PDF: %PDF-），"
                            "文本类文件执行可解码性检查；3) 魔数与扩展名不一致一律返回 400 拒绝；"
                            "4) 有条件时接入成熟的内容识别库并对文件重命名为服务端生成的安全名称。"
                        ),
                    },
                }
            )

    # ---------- 4. 低危信息泄露：响应中回传服务器绝对路径
    def test_path_disclosure(self):
        print("[5/5] 低危项检查：响应是否回传服务器绝对路径 ...")
        probe_name = f"{PROBE_PREFIX}{self.token}_disclosure.txt"
        status, payload = self.upload(probe_name, f"disclosure check {self.token}".encode("utf-8"))
        disclosed = []
        for key in ("saved_path", "real_path"):
            value = payload.get(key)
            if isinstance(value, str) and (os.path.isabs(value) or ":\\" in value or value.startswith("/")):
                disclosed.append({"field": key, "value": value})
        self.checks.append(
            {
                "check": "响应绝对路径信息泄露",
                "submitted_filename": probe_name,
                "http_status": status,
                "result": "存在" if disclosed else "未发现",
            }
        )
        if disclosed:
            self.findings.append(
                {
                    "id": "ABSOLUTE_PATH_DISCLOSURE",
                    "vulnerability_type": "服务器绝对路径信息泄露",
                    "risk_level": "低",
                    "title": "上传成功响应向客户端回传服务器本地绝对路径，便于攻击者构造遍历路径",
                    "status": "confirmed",
                    "affected_paths": [item["value"] for item in disclosed],
                    "evidence": disclosed,
                    "detected_at": now_iso(),
                    "remediation": {
                        "path_normalization": (
                            "响应中只返回服务端生成的相对资源标识，不回传 realpath/abspath 等本地绝对路径；"
                            "内部仍须保留 basename + realpath + commonpath 的规范化与边界校验。"
                        ),
                        "content_type_validation": (
                            "保持扩展名白名单与魔数双重校验，避免泄露的路径信息被结合类型绕过利用。"
                        ),
                    },
                }
            )

    # ---------- 清理
    def cleanup_start(self):
        os.makedirs(self.upload_dir, exist_ok=True)
        os.makedirs(self.sandbox_root, exist_ok=True)
        # 清理历史探测残留：上传目录递归、沙箱根仅顶层（避免触碰无关子目录）
        self.removed_at_start.extend(remove_probe_files(self.upload_dir, recursive=True))
        self.removed_at_start.extend(remove_probe_files(self.sandbox_root, recursive=False))

    def cleanup_end(self):
        # 上传目录内本次产生的所有测试文件必须清理
        self.removed_at_end.extend(remove_probe_files(self.upload_dir, recursive=True, token=self.token))
        if self.clean_evidence:
            for evidence in list(self.retained_evidence):
                try:
                    os.remove(evidence)
                    self.removed_at_end.append(evidence)
                except OSError:
                    pass
            self.retained_evidence = []
        else:
            # 仅保留确实仍存在于上传目录之外的漏洞证据
            self.retained_evidence = [
                path for path in dict.fromkeys(self.retained_evidence) if os.path.isfile(path)
            ]

    # ---------- 报告
    def write_report(self):
        self.findings.sort(key=lambda item: RISK_ORDER[item["risk_level"]], reverse=True)
        high = sum(1 for f in self.findings if f["risk_level"] == "高")
        medium = sum(1 for f in self.findings if f["risk_level"] == "中")
        low = sum(1 for f in self.findings if f["risk_level"] == "低")
        report = {
            "report_version": "1.0",
            "generated_at": now_iso(),
            "scan_token": self.token,
            "target": {
                "base_url": self.base_url,
                "upload_dir": self.upload_dir,
                "sandbox_root": self.sandbox_root,
            },
            "summary": {
                "total_findings": len(self.findings),
                "high_risk": high,
                "medium_risk": medium,
                "low_risk": low,
                "vulnerable": bool(high or medium),
            },
            "findings": self.findings,
            "probe_checks": self.checks,
            "warnings": self.warnings,
            "confinement": {
                "rule": "所有探测文件名的预期落点必须位于上传目录或其父级测试沙箱内，发送前强制断言",
                "upload_dir": self.upload_dir,
                "sandbox_root": self.sandbox_root,
                "all_probe_paths_within_sandbox": True,
            },
            "cleanup": {
                "removed_stale_files_at_start": self.removed_at_start,
                "removed_test_files_at_end": self.removed_at_end,
                "retained_evidence_outside_upload_dir": self.retained_evidence,
                "note": (
                    "上传目录内测试文件已全部清理；上传目录之外仅保留漏洞证据文件，"
                    "加 --clean-evidence 可在运行结束时一并删除。"
                ),
            },
        }
        with open(self.report_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        return report

    def run(self):
        # 任何情况下先移除旧报告，保证失败运行不会留下误导性 report.json
        if os.path.isfile(self.report_path):
            os.remove(self.report_path)

        rc = self.preflight()
        if rc:
            return rc
        self.cleanup_start()
        rc = self.baseline()
        if rc:
            return rc
        self.test_path_traversal()
        self.test_type_bypass()
        self.test_path_disclosure()
        self.cleanup_end()
        report = self.write_report()

        print("\n================ 扫描结论 ================")
        for finding in report["findings"]:
            print(f"[{finding['risk_level']}] {finding['vulnerability_type']}：{finding['title']}")
            for path in finding["affected_paths"]:
                print(f"      受影响路径: {path}")
        print(
            f"\n合计 {report['summary']['total_findings']} 项（高 {report['summary']['high_risk']} / "
            f"中 {report['summary']['medium_risk']} / 低 {report['summary']['low_risk']}）"
        )
        print(f"报告已写入（UTF-8 JSON）: {self.report_path}")
        if self.retained_evidence:
            print("上传目录之外保留的漏洞证据（可加 --clean-evidence 自动删除）:")
            for path in self.retained_evidence:
                print(f"      {path}")
        return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="本机上传服务安全审计工具（路径遍历写入 / 类型绕过探测），仅限 127.0.0.1 授权目标。"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="上传服务基址，默认 http://127.0.0.1:8000")
    parser.add_argument(
        "--dir",
        default=os.path.join(os.getcwd(), "uploads"),
        help="服务上传目录（绝对/相对路径），默认 ./uploads；工具据此判定越界落点",
    )
    parser.add_argument(
        "--sandbox-root",
        default=None,
        help="测试沙箱根目录，默认为上传目录的父目录；所有探测写入不得超出该目录",
    )
    parser.add_argument("--report", default=os.path.join(os.getcwd(), "report.json"), help="JSON 报告输出路径")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP 超时秒数，默认 5")
    parser.add_argument(
        "--clean-evidence",
        action="store_true",
        help="运行结束时一并删除上传目录之外的漏洞证据文件（默认保留以便人工核验）",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return Auditor(args).run()


if __name__ == "__main__":
    sys.exit(main())
