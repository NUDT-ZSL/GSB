"""越权读取检测入口。

对授权的本地预览服务发起路径穿越尝试，判断是否读到 uploads 之外的文件，
并把结果以字段固定的 JSON 报告写入本地文件。

报告固定字段：
  target     - 被检测服务地址
  conclusion - VULNERABLE / NOT_VULNERABLE
  evidence   - 证据摘要列表，每项固定字段 check/request/status/finding

用法: python detector.py <base_url> [--out 报告路径]
示例: python detector.py http://127.0.0.1:18081 --out reports/vulnerable_report.json
"""
import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from common import BASE, SECRET_MARKER, outside_relative_name

EVIDENCE_FIELDS = ("check", "request", "status", "finding")


def fetch(base, name):
    """请求 /preview，返回 (status, body)。网络/HTTP 错误都转成可读结果。"""
    url = base.rstrip("/") + "/preview?name=" + quote(name)
    try:
        with urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except URLError as e:
        return 0, "connection error: %s" % e.reason


def detect(base):
    """运行检测，返回固定字段的报告 dict。"""
    evidence = []

    # 1) 基线：正常文件应可读，确认服务在线且行为正常
    st, body = fetch(base, "hello.txt")
    evidence.append({
        "check": "baseline",
        "request": "name=hello.txt",
        "status": st,
        "finding": "service online, normal file readable" if st == 200
                   else "unexpected: normal file not readable",
    })

    # 2) 越权尝试：读取 uploads 之外的样本
    traversal = outside_relative_name()  # 例如 ../outside/secret.txt
    st, body = fetch(base, traversal)
    reachable = st == 200 and SECRET_MARKER in body
    evidence.append({
        "check": "traversal",
        "request": "name=" + traversal,
        "status": st,
        "finding": "outside file readable (secret marker returned)" if reachable
                   else "outside file not readable",
    })

    # 3) 泄露检查：穿越响应不得泄露目录外内容或本机绝对路径
    leaks = []
    if SECRET_MARKER in body:
        leaks.append("outside file content")
    if BASE.replace("/", "\\") in body or BASE.replace("\\", "/") in body:
        leaks.append("absolute path")
    evidence.append({
        "check": "no_leak_on_reject",
        "request": "name=" + traversal,
        "status": st,
        "finding": "response leaks: " + ", ".join(leaks) if leaks
                   else "no outside content or absolute path leaked",
    })

    return {
        "target": base,
        "conclusion": "VULNERABLE" if reachable else "NOT_VULNERABLE",
        "evidence": evidence,
    }


def write_report(report, out_path):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="本地预览服务越权读取检测")
    ap.add_argument("base", help="被检测服务地址，如 http://127.0.0.1:18081")
    ap.add_argument("--out", default=None, help="报告输出路径（默认打印到 stdout）")
    args = ap.parse_args()

    report = detect(args.base)
    if args.out:
        write_report(report, args.out)
        print("报告已写入: %s" % args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["conclusion"] == "NOT_VULNERABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
