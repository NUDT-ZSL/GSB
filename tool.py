#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tool.py — 离线 SQL 注入检测与参数化修复工具

读取 JSON 查询描述（query_template / params / safe），检测两类风险：
  1. 模板拼接：safe=false 且参数值被直接拼入 SQL 模板（f-string / 加号拼接特征）
  2. 参数载荷：参数值含有闭合引号、注释符、堆叠查询分号等注入载荷

输出 JSON 报告（detections + fixed_query），对 safe=false 的查询生成
参数化后的安全查询描述；对含占位符语法的模板做修复前后模拟执行一致性验证。

仅使用 Python 标准库，不连接数据库、不访问网络，Windows 下可直接运行。

用法:
  python tool.py <输入.json> <输出.json>     分析单个查询描述并生成 JSON 报告
  python tool.py --generate-samples [目录]   生成内置样本（默认 samples/）
"""
import json
import os
import re
import sys

SEVERITY_HIGH = "high"  # 高：可直接利用的注入（模板拼接 / 强载荷特征）
SEVERITY_LOW = "low"    # 低：可疑但不足以直接利用的弱特征

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 强载荷特征 → high（按优先级排序，命中即停）
STRONG_PAYLOAD_RULES = [
    ("quote_escape",
     re.compile(r"'\s*(?:or|and)\b", re.IGNORECASE),
     "闭合字符串引号并拼接布尔逻辑（典型载荷如 ' OR '1'='1）"),
    ("stacked_query",
     re.compile(r";\s*(?:drop|delete|update|insert|select|alter|create|truncate|exec|execute)\b",
                re.IGNORECASE),
     "分号堆叠查询：在原语句之后追加执行新的 SQL 语句"),
    ("comment",
     re.compile(r"--|/\*|\*/"),
     "SQL 注释符（--、/*、*/）：截断后续语句以绕过原有条件"),
]

# 弱载荷特征 → low
WEAK_PAYLOAD_RULES = [
    ("suspicious_keyword",
     re.compile(r"\bunion\b\s+(?:all\s+)?select\b", re.IGNORECASE),
     "UNION SELECT 关键字组合，疑似联合查询注入"),
    ("suspicious_keyword",
     re.compile(r"\b(?:or|and)\s+\d+\s*=\s*\d+", re.IGNORECASE),
     "恒真条件（如 OR 1=1），疑似绕过过滤"),
    ("suspicious_keyword",
     re.compile(r"\bdrop\s+table\b|\bsleep\s*\(|\bbenchmark\s*\(", re.IGNORECASE),
     "危险 SQL 关键字（DROP TABLE / 延时函数）"),
    ("lone_quote",
     re.compile(r"^[^']*'[^']*$"),
     "单个未配对引号，疑似试图破坏 SQL 字符串结构"),
]

REQUIRED_FIELDS = ("query_template", "params", "safe")


# ---------------------------------------------------------------- 基础检查

def _value_to_sql_text(value):
    """参数值在 SQL 文本中的形态：字符串原样，数字转十进制文本，其余不参与拼接检测。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _boundary_pattern(text):
    """值在模板中的匹配需落在非字母数字边界上，避免子串误报（如 paid 命中 unpaid）。"""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(text) + r"(?![A-Za-z0-9_])")


def check_quote_balance(template):
    """检查模板引号是否配对（SQL 以两个连续单引号转义一个字面单引号）。
    返回 None 表示配对，否则返回原因说明。"""
    in_single = False
    in_double = False
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if in_single:
            if ch == "'":
                if i + 1 < n and template[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
        i += 1
    if in_single:
        return "单引号未闭合"
    if in_double:
        return "双引号未闭合"
    return None


# ---------------------------------------------------------------- 检测

def detect_concatenation(template, params):
    """模板拼接检测：参数值直接出现在模板文本中（f-string / 加号拼接的渲染结果）。
    location 为模板偏移（0 起始的字符位置）。"""
    results = []
    for name in sorted(params):
        text = _value_to_sql_text(params[name])
        if not text:
            continue
        m = _boundary_pattern(text).search(template)
        if m:
            results.append({
                "type": "template_concatenation",
                "severity": SEVERITY_HIGH,
                "location": m.start(),
                "param": name,
                "reason": "模板拼接：参数 '%s' 的值被直接拼入 SQL 模板"
                          "（f-string/加号拼接特征），攻击者可借该参数注入 SQL" % name,
            })
    return results


def detect_payloads(params):
    """参数载荷检测：参数值含闭合引号、注释符、堆叠查询分号等 → high；
    弱特征（UNION SELECT、恒真条件、危险关键字、孤立引号）→ low。
    location 为参数名。"""
    results = []
    for name in sorted(params):
        value = params[name]
        if not isinstance(value, str):
            continue
        matched = None
        for ptype, rx, desc in STRONG_PAYLOAD_RULES:
            if rx.search(value):
                matched = (SEVERITY_HIGH, ptype, desc)
                break
        if matched is None:
            for ptype, rx, desc in WEAK_PAYLOAD_RULES:
                if rx.search(value):
                    matched = (SEVERITY_LOW, ptype, desc)
                    break
        if matched:
            severity, ptype, desc = matched
            results.append({
                "type": "parameter_payload",
                "severity": severity,
                "location": name,
                "payload_type": ptype,
                "reason": "参数载荷：参数 '%s' 的值%s" % (name, desc),
            })
    return results


def _detection_sort_key(d):
    """high 在前；同级中拼接项按模板偏移升序、载荷项按参数名升序；拼接项排在载荷项之前。"""
    sev_rank = 0 if d["severity"] == SEVERITY_HIGH else 1
    type_rank = 0 if d["type"] == "template_concatenation" else 1
    loc = d["location"]
    return (sev_rank, type_rank, loc if isinstance(loc, int) else -1, str(loc))


# ---------------------------------------------------------------- 修复与验证

def build_fixed_query(template, params):
    """生成参数化描述：把拼入模板的参数值替换为 {参数名} 占位符，
    模板仅保留占位符，参数集中存放且保留原参数键值。"""
    fixed = template
    items = []
    for name, value in params.items():
        text = _value_to_sql_text(value)
        if text:
            items.append((name, text))
    # 先替换较长的值，避免短值是长值子串时误替换
    items.sort(key=lambda kv: len(kv[1]), reverse=True)
    for name, text in items:
        fixed = _boundary_pattern(text).sub("{" + name + "}", fixed)
    return {"query_template": fixed, "params": dict(params), "safe": True}


def simulate(template, params):
    """模拟执行：把 {name} 占位符替换为参数值，得到最终 SQL 文本。"""
    return PLACEHOLDER_RE.sub(lambda m: str(params[m.group(1)]), template)


# ---------------------------------------------------------------- 主分析

def _error_report(message):
    return {"error": message, "detections": [], "fixed_query": None, "validation": None}


def analyze(desc):
    """分析单条查询描述，返回报告字典（不含 input 字段）。"""
    if not isinstance(desc, dict):
        return _error_report("查询描述必须是 JSON 对象")
    missing = [f for f in REQUIRED_FIELDS if f not in desc]
    if missing:
        return _error_report("缺少必需字段: " + ", ".join(missing))
    template = desc["query_template"]
    params = desc["params"]
    safe = desc["safe"]
    if not isinstance(template, str):
        return _error_report("字段 query_template 必须是字符串")
    if not isinstance(params, dict):
        return _error_report("字段 params 必须是对象")
    if not isinstance(safe, bool):
        return _error_report("字段 safe 必须是布尔值")

    # 模板引用的占位符必须都有对应参数
    referenced = sorted(set(PLACEHOLDER_RE.findall(template)))
    missing_params = [n for n in referenced if n not in params]
    if missing_params:
        return _error_report("缺少参数: " + ", ".join(missing_params))

    # 模板引号必须配对
    quote_err = check_quote_balance(template)
    if quote_err:
        return _error_report("模板引号不配对: " + quote_err)

    detections = []
    fixed_query = None
    validation = None
    has_placeholders = bool(referenced)

    if safe:
        # safe=true：不做拼接检测与载荷检测，仅对占位符模板做模拟执行验证
        if has_placeholders:
            simulated = simulate(template, params)
            validation = {
                "placeholder_template": True,
                "simulated_before": simulated,
                "simulated_after": simulated,
                "consistent": True,
                "detail": "占位符参数替换成功，替换后的字段值相等",
            }
    else:
        detections.extend(detect_concatenation(template, params))
        detections.extend(detect_payloads(params))
        detections.sort(key=_detection_sort_key)
        fixed_query = build_fixed_query(template, params)
        if has_placeholders:
            # 仅对已含占位符语法的模板：验证修复前后模拟执行结果一致
            before = simulate(template, params)
            after = simulate(fixed_query["query_template"], fixed_query["params"])
            consistent = before == after
            validation = {
                "placeholder_template": True,
                "simulated_before": before,
                "simulated_after": after,
                "consistent": consistent,
                "detail": ("修复前后模拟执行结果一致，替换后的字段值相等"
                           if consistent else "修复前后模拟执行结果不一致"),
            }

    return {
        "error": None,
        "safe": safe,
        "detections": detections,
        "counts": {
            "high": sum(1 for d in detections if d["severity"] == SEVERITY_HIGH),
            "low": sum(1 for d in detections if d["severity"] == SEVERITY_LOW),
        },
        "fixed_query": fixed_query,
        "validation": validation,
    }


def analyze_file(path):
    """读取 JSON 描述文件并分析；文件损坏或 JSON 非法时报告含 error 字段。"""
    report = {"input": path}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        report.update(_error_report("无法读取输入文件: %s" % e))
        return report
    try:
        desc = json.loads(raw)
    except json.JSONDecodeError as e:
        report.update(_error_report("JSON 解析失败: %s" % e))
        return report
    report.update(analyze(desc))
    return report


# ---------------------------------------------------------------- 内置样本生成器

SAMPLE_DESCRIPTIONS = [
    # —— 类别一：拼接漏洞（safe=false，参数值直接拼入模板）——
    ("sample_concat_fstring.json", {
        "description": "拼接漏洞：f-string 风格，用户名与状态被直接拼入 SQL",
        "query_template": "SELECT * FROM orders WHERE username = 'alice' AND status = 'paid'",
        "params": {"username": "alice", "status": "paid"},
        "safe": False,
    }),
    ("sample_concat_plus.json", {
        "description": "拼接漏洞：加号拼接风格，订单 id 与渠道被直接拼入 SQL",
        "query_template": "SELECT * FROM orders WHERE id = 1042 AND channel = 'web'",
        "params": {"id": 1042, "channel": "web"},
        "safe": False,
    }),
    # —— 类别二：载荷注入（参数值携带恶意载荷）——
    ("sample_payload_quote.json", {
        "description": "载荷注入：闭合引号 + 恒真条件 ' OR '1'='1",
        "query_template": "SELECT * FROM orders WHERE username = '' OR '1'='1' AND status = 'paid'",
        "params": {"username": "' OR '1'='1", "status": "paid"},
        "safe": False,
    }),
    ("sample_payload_comment.json", {
        "description": "载荷注入：注释符截断后续条件",
        "query_template": "SELECT * FROM orders WHERE id = 1 OR 1=1-- AND status = 'paid'",
        "params": {"id": "1 OR 1=1--", "status": "paid"},
        "safe": False,
    }),
    ("sample_payload_stacked.json", {
        "description": "载荷注入：分号堆叠查询 DROP TABLE",
        "query_template": "SELECT * FROM orders WHERE id = 1; DROP TABLE orders; --",
        "params": {"id": "1; DROP TABLE orders; --"},
        "safe": False,
    }),
    # —— 类别三：安全查询（safe=true，占位符模板）——
    ("sample_safe.json", {
        "description": "安全查询：占位符模板 + 正常参数",
        "query_template": "SELECT * FROM orders WHERE username = '{username}' AND status = '{status}'",
        "params": {"username": "alice", "status": "paid"},
        "safe": True,
    }),
    ("sample_safe_malicious_param.json", {
        "description": "安全查询：占位符模板 + 恶意参数（参数化执行，载荷无法逃逸）",
        "query_template": "SELECT * FROM orders WHERE username = '{username}' AND status = '{status}'",
        "params": {"username": "' OR '1'='1", "status": "paid"},
        "safe": True,
    }),
    # —— 边界与排序样本 ——
    ("sample_mixed_severity.json", {
        "description": "混合样本：两处拼接（high）+ 两个弱载荷参数（low），验证排序",
        "query_template": "SELECT * FROM orders WHERE username = 'alice' AND status = 'paid'",
        "params": {"username": "alice", "status": "paid",
                   "remark_b": "OR 1=1", "remark_a": "UNION SELECT password"},
        "safe": False,
    }),
    ("sample_placeholder_mixed.json", {
        "description": "混合写法：模板已有 {id} 占位符，username 仍被拼接；验证修复前后模拟执行一致",
        "query_template": "SELECT * FROM orders WHERE id = {id} AND username = 'alice'",
        "params": {"id": 7, "username": "alice"},
        "safe": False,
    }),
    ("sample_unbalanced_quote.json", {
        "description": "错误样本：模板单引号未闭合",
        "query_template": "SELECT * FROM orders WHERE username = 'alice",
        "params": {"username": "alice"},
        "safe": False,
    }),
    ("sample_missing_param.json", {
        "description": "错误样本：模板引用未提供的参数 email",
        "query_template": "SELECT * FROM orders WHERE username = '{username}' AND email = '{email}'",
        "params": {"username": "alice"},
        "safe": True,
    }),
    ("sample_missing_field.json", {
        "description": "错误样本：缺少 safe 字段",
        "query_template": "SELECT * FROM orders WHERE id = {id}",
        "params": {"id": 1},
    }),
]

BAD_JSON_TEXT = '{"query_template": "SELECT 1", "params": {}, "safe": true,,,}'


def generate_samples(outdir):
    """生成内置样本：拼接漏洞、载荷注入、安全查询三类，外加错误/排序边界样本。"""
    os.makedirs(outdir, exist_ok=True)
    written = []
    for fname, desc in SAMPLE_DESCRIPTIONS:
        path = os.path.join(outdir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(desc, f, ensure_ascii=False, indent=2)
        written.append(path)
    bad = os.path.join(outdir, "sample_bad_json.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write(BAD_JSON_TEXT)
    written.append(bad)
    return written


# ---------------------------------------------------------------- 命令行

USAGE = __doc__


def main(argv):
    if len(argv) >= 2 and argv[1] == "--generate-samples":
        outdir = argv[2] if len(argv) > 2 else "samples"
        files = generate_samples(outdir)
        print("已生成 %d 个样本文件到 %s:" % (len(files), os.path.abspath(outdir)))
        for p in files:
            print("  " + p)
        return 0

    if len(argv) != 3:
        sys.stderr.write(USAGE)
        return 2

    inp, outp = argv[1], argv[2]
    if not os.path.isfile(inp):
        sys.stderr.write("输入文件不存在: %s\n" % inp)
        return 1

    report = analyze_file(inp)
    try:
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except OSError as e:
        sys.stderr.write("无法写入报告: %s\n" % e)
        return 1

    if report.get("error"):
        print("分析完成（发现错误）: %s -> %s" % (inp, outp))
        print("  error: %s" % report["error"])
    else:
        c = report["counts"]
        print("分析完成: %s -> %s" % (inp, outp))
        print("  检测项: high=%d low=%d  fixed_query=%s" % (
            c["high"], c["low"], "已生成" if report["fixed_query"] else "null"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
