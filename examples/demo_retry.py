"""演示：配置文件首次读取被拒绝（模拟占用/权限瞬时问题），工具提示后重试成功。

运行：python -I examples/demo_retry.py
全程离线，仅使用标准库。
"""

import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cmd_guard.engine import build_rule_set, load_sources  # noqa: E402

VALID_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "samples", "input", "rules_valid.json",
)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="cmd_guard_retry_")
    locked = os.path.join(tmpdir, "locked_once.json")
    with open(locked, "w", encoding="utf-8") as fh:
        fh.write(open(VALID_CONFIG, "r", encoding="utf-8").read())

    state = {"attempts": 0, "handle": None}

    def flaky_read(path: str) -> str:
        if os.path.abspath(path) != os.path.abspath(locked):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        state["attempts"] += 1
        if state["attempts"] == 1:
            # 首次读取：以独占方式打开文件，模拟其它进程占用导致 PermissionError
            state["handle"] = open(locked, "r+", encoding="utf-8")
            state["handle"].write  # noqa: B018 - 仅保持句柄占用
            raise PermissionError("模拟文件被其它进程独占占用")
        # 第二次读取：释放占用后恢复正常
        if state["handle"] is not None:
            state["handle"].close()
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    print(f"[demo] 瞬时不可读文件: {locked}")
    result = load_sources([locked], retry_attempts=2, read_func=flaky_read)

    assert result.missing_inputs == [], result.missing_inputs
    assert locked in result.retried_paths
    rule_set = build_rule_set(result)
    ids = [r.id for r in rule_set.records]
    print(f"[demo] 重试后成功读取，retried_paths={result.retried_paths}")
    print(f"[demo] 完整评估的规则 id: {ids}")
    assert ids == ["R001", "R002", "R003", "R004"]
    print("[demo] OK：瞬时失败经重试恢复，规则数据完整可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
