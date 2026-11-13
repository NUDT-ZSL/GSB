"""构建确定性样本与配置（每次运行生成字节一致的样本）。

布局::

    samples/raw/      原始“有漏洞”样本（含损坏文件、非数据文件）
    samples/fixed/    修复副本（保留部分未修复项用于对比演示）
    config/           合法自定义规则与损坏配置
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
RAW = SAMPLES / "raw"
FIXED = SAMPLES / "fixed"
CONFIG = ROOT / "config"

# 带 BOM 的非法 UTF-8 字节序列
BAD_UTF8_BYTES = b"\xff\xfe\x00\x10name,email\n\x80\x81 broken\xc3\x28 row\n"
# JSON 语法损坏（缺右括号）
BROKEN_JSON = '{"users": [{"id": 1, "name": "破损文件"\n'


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def build_raw() -> None:
    users = {
        "users": [
            {"id": 1, "name": "张三", "remark": "普通备注，没有危险内容"},
            {"id": 2, "name": "李四",
             "remark": "admin'; DROP TABLE users; -- 高危 SQL 注入"},
            {"id": 3, "name": "王五",
             "login_hint": "x' OR '1'='1 永真式注入",
             "note": "1 UNION SELECT password FROM accounts"},
        ]
    }
    _write_json(RAW / "users.json", users)

    orders_csv = (
        "order_id,customer,note\n"
        "1001,赵六,正常订单备注\n"
        "1002,钱七,备份; cat /etc/passwd 命令注入\n"
        "1003,孙八,正常文本 hello world\n"
    )
    _write_text(RAW / "orders.csv", orders_csv)

    profile = {
        "profile": {
            "nickname": "安全昵称",
            "bio": "例行维护 && rm -rf /tmp/old 命令链特征",
            "debug": "1; SELECT SLEEP(5) 自定义规则命中项",
            "homepage": "https://example.com/~user 普通链接不命中",
        }
    }
    _write_json(RAW / "profile.json", profile)

    clean = {
        "departments": [
            {"id": 1, "title": "研发部", "desc": "负责产品研发"},
            {"id": 2, "title": "市场部", "desc": "contact@example.com 普通邮箱文本"},
        ]
    }
    _write_json(RAW / "departments.json", clean)

    # 损坏文件 1：非 UTF-8
    (RAW / "broken_encoding.csv").write_bytes(BAD_UTF8_BYTES)
    # 损坏文件 2：JSON 语法错误
    _write_text(RAW / "broken_syntax.json", BROKEN_JSON)
    # 合法但扩展名不允许的文件
    _write_text(RAW / "notes.txt", "此文件不应被解析\n")


def build_fixed() -> None:
    if FIXED.exists():
        shutil.rmtree(FIXED)
    shutil.copytree(RAW, FIXED)

    # 修复 users.json：参数化查询改造后，输入中的危险片段被白名单转义处理
    fixed_users = {
        "users": [
            {"id": 1, "name": "张三", "remark": "普通备注，没有危险内容"},
            {"id": 2, "name": "李四",
             "remark": "admin''；危险 SQL 关键字已被输入校验拦截并转义"},
            {"id": 3, "name": "王五",
             "login_hint": "输入校验拦截：永真式表达式不允许出现",
             "note": "联合查询关键字已被过滤，改为普通文本"},
        ]
    }
    _write_json(FIXED / "users.json", fixed_users)

    # 修复 profile.json：全部命令/SQL 特征清除
    fixed_profile = {
        "profile": {
            "nickname": "安全昵称",
            "bio": "例行维护已改为平台定时任务，不再接受命令输入",
            "debug": "调试字段已移除动态 SQL，仅保留静态标识",
            "homepage": "https://example.com/~user 普通链接不命中",
        }
    }
    _write_json(FIXED / "profile.json", fixed_profile)

    # orders.csv 中保留 1002 的命令注入（演示“仍存在”），其余不动
    orders_csv = (
        "order_id,customer,note\n"
        "1001,赵六,正常订单备注\n"
        "1002,钱七,备份; cat /etc/passwd 命令注入\n"
        "1003,孙八,正常文本 hello world\n"
    )
    _write_text(FIXED / "orders.csv", orders_csv)


def build_configs() -> None:
    good = {
        "version": 1,
        "patterns": [
            {
                "name": "custom_sql_sleep",
                "type": "sql_injection",
                "risk": "medium",
                "pattern": r"\bSELECT\s+SLEEP\s*\(",
                "advice": "禁止在用户输入中出现延时函数，改用参数化查询。",
            },
            {
                "name": "custom_xss_script",
                "type": "xss",
                "risk": "high",
                "pattern": "<script>",
                "literal": True,
                "advice": "对 HTML 上下文做实体转义，或使用净化库。",
            },
        ],
    }
    _write_json(CONFIG / "rules.json", good)
    # 损坏配置：JSON 语法错误
    _write_text(CONFIG / "rules_broken.json", '{"patterns": [ {"name": "oops", }\n')
    # 部分错误配置：语法正确但含非法条目，验证“坏条目跳过、好条目生效”
    partial = {
        "patterns": [
            {"name": "bad_no_pattern", "type": "sql_injection", "risk": "high"},
            {"name": "custom_xss_script", "type": "xss", "risk": "high",
             "pattern": "<script>", "literal": True},
        ]
    }
    _write_json(CONFIG / "rules_partial.json", partial)


def main() -> None:
    if SAMPLES.exists():
        shutil.rmtree(SAMPLES)
    if CONFIG.exists():
        shutil.rmtree(CONFIG)
    RAW.mkdir(parents=True)
    CONFIG.mkdir(parents=True)
    build_raw()
    build_fixed()
    build_configs()
    print(f"样本已生成: {RAW}")
    print(f"修复副本:   {FIXED}")
    print(f"配置目录:   {CONFIG}")


if __name__ == "__main__":
    main()
