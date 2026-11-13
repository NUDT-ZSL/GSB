#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_reports.py — 对 ssti_probe 产物做自动化验收检查。"""
import csv
import json
from datetime import datetime

# 要求12：日志含每个目标起止行，且不含完整响应体
log = open("probe.log", encoding="utf-8").read()
for tid in ["t-normal", "t-vuln", "t-suspect", "t-error", "t-big", "t-slow", "t-headers"]:
    assert ("开始检测目标 id=%s" % tid) in log, tid + " 缺起始行"
    assert ("目标 id=%s 检测结束" % tid) in log, tid + " 缺结束行"
print("要求12 日志含每个目标起止行: OK")
for body in ["结果:49", "欢迎访问演示页面", "B" * 500, "S" * 110]:
    assert body not in log, "日志泄露响应体: " + body[:20]
print("要求12 日志不含完整响应体: OK")

# 要求8：状态取值限于规定集合
rep = json.load(open("report.json", encoding="utf-8"))
allowed = {"存在漏洞", "疑似漏洞", "未发现漏洞", "响应异常", "超时"}
statuses = {t["status"] for t in rep["targets"]}
assert statuses <= allowed, statuses
print("要求8  状态集合合法: OK ->", sorted(statuses))

# 要求1/9：报告含全部目标 id/url/status 及逐载荷结果
for t in rep["targets"]:
    assert all(k in t for k in ("id", "url", "status", "payload_results")), t
assert rep["targets"][0]["id"] == "t-normal"
assert rep["targets"][0]["url"] == "http://127.0.0.1:8377/normal"
print("要求1/9 JSON含目标id/url/status及逐载荷结果: OK (目标数=%d)" % len(rep["targets"]))

# 要求14：缺 url 的目标不出现在报告中
assert "t-no-url" not in [t["id"] for t in rep["targets"]]
print("要求14 缺字段目标已跳过: OK")

# 要求10：CSV 四列 + ISO8601 可解析
rows = list(csv.reader(open("report.csv", encoding="utf-8")))
assert rows[0] == ["目标ID", "URL", "状态", "发现时间"], rows[0]
for r in rows[1:]:
    datetime.fromisoformat(r[3])
print("要求10 CSV四列+ISO8601时间: OK (%d 行数据)" % (len(rows) - 1))

# 要求15：UTF-8、JSON 两格缩进、CSV 逗号分隔
raw = open("report.json", "rb").read()
raw.decode("utf-8")
assert raw.startswith(b'{\n  "tool"'), "JSON 缩进不是两格"
line = open("report.csv", "rb").read().decode("utf-8").splitlines()[1]
assert line.count(",") == 3, "CSV 分隔符异常"
print("要求15 UTF-8/两格缩进/逗号分隔: OK")

# 要求11：捕获请求头中 User-Agent 为约定值且无其他自定义头
headers = json.load(open("captured_headers.json", encoding="utf-8"))
assert headers.get("User-Agent") == "SSTI-LocalProbe/1.0 (+authorized-local-only)"
standard = {"Host", "Accept-Encoding", "Connection", "User-Agent"}
assert set(headers) <= standard, set(headers) - standard
print("要求11 仅约定User-Agent, 无其他自定义头: OK ->", sorted(headers))

print("\n全部自动验收检查通过。")
