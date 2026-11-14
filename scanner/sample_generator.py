"""确定性样本生成器。

由代码固定生成配置/响应样本、修复版样本、损坏样本与样本清单，
不使用时间戳、随机数或网络访问，保证任何机器、任意次数运行产物一致。

生成布局：
<target>/
  manifest.json
  samples/            修复前样本（含损坏样本与缺失声明）
  fixed_samples/      声称已修复的对应样本（仅含可修复的正常样本）
"""

from __future__ import annotations

import json
from pathlib import Path

SAMPLES_DIR = "samples"
FIXED_DIR = "fixed_samples"

# ---------------------------------------------------------------------------
# 固定样本内容
# ---------------------------------------------------------------------------

S01_HEADERS = {
    "Content-Type": "application/json",
    "Server": "nginx/1.22.1",
    "X-Powered-By": "PHP/8.1.2",
}
S01_BODY = {
    "service": "auth-center",
    "endpoints": {"/login": "POST", "/token": "POST"},
}

S02_BODY = {
    "user": "ops-admin",
    "password": "Pl@inText-Pwd-2026",
    "nested": {"token": "tok_9f3a2c7e1b6d4801"},
    "backup": {"token": "tok_57be02d9aa417c03"},
}
S02_HEADERS = {"Content-Type": "application/json", "Server": "nginx"}

S03_HEADERS = {"Content-Type": "application/json"}
S03_BODY = {"status": "healthy", "checks": {"db": "up", "cache": "up"}}

S04_HEADERS = {"Content-Type": "text/plain"}
S04_BODY = (
    "debug log export\n"
    "operator note: found a key shaped like AKIAIOSFODNN7EXAMPLE in old notes.\n"
    "another line: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVC\n"
    "end of log\n"
)

S05_CORRUPT_BYTES = b'{"sample_id": "S05", "response": {\n "headers": {,}\n broken json'

S07_BODY = {"user": "demo", "password": "should-not-be-scanned"}

# ---------------------------------------------------------------------------
# 修复版内容：移除/替换对应暴露
# ---------------------------------------------------------------------------

S01_FIXED_HEADERS = {"Content-Type": "application/json"}
S01_FIXED_BODY = dict(S01_BODY)

S02_FIXED_BODY = {
    "user": "ops-admin",
    "credential_ref": "vault://auth-center/ops-admin/password",
    "nested": {"token_ref": "vault://auth-center/token-a"},
    "backup": {"token_ref": "vault://auth-center/token-b"},
}
# 修复同时移除无版本的 Server 标识头，避免残留低危暴露
S02_FIXED_HEADERS = {"Content-Type": "application/json"}

S03_FIXED_HEADERS = dict(S03_HEADERS)
S03_FIXED_BODY = dict(S03_BODY)

S04_FIXED_HEADERS = dict(S04_HEADERS)
S04_FIXED_BODY = (
    "debug log export\n"
    "operator note: old key material was removed and rotated.\n"
    "authorization header was stripped from this export.\n"
    "end of log\n"
)


def _sample(headers: dict[str, str], body: object) -> dict[str, object]:
    return {
        "sample_version": 1,
        "response": {"headers": headers, "body": body},
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")


def generate(target: Path) -> list[Path]:
    """在 target 下生成全套确定性样本，返回写入的文件路径列表（固定顺序）。"""

    written: list[Path] = []
    samples = target / SAMPLES_DIR
    fixed = target / FIXED_DIR

    layout = [
        ("S01_server_version.json", _sample(S01_HEADERS, S01_BODY)),
        ("S02_plaintext_credential.json", _sample(S02_HEADERS, S02_BODY)),
        ("S03_clean.json", _sample(S03_HEADERS, S03_BODY)),
        ("S04_suspect_text.json", _sample(S04_HEADERS, S04_BODY)),
        ("S07_excluded.json", _sample(S03_HEADERS, S07_BODY)),
    ]
    for filename, payload in layout:
        destination = samples / filename
        _write_json(destination, payload)
        written.append(destination)

    # 损坏样本：直接写字节，保证其确实无法被 JSON 解析
    corrupt = samples / "S05_corrupt.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(S05_CORRUPT_BYTES)
    written.append(corrupt)
    # S06 声明纳入但故意不生成文件，用于验证“样本缺失 → 未完成”

    fixed_layout = [
        ("S01_server_version.fixed.json", _sample(S01_FIXED_HEADERS, S01_FIXED_BODY)),
        ("S02_plaintext_credential.fixed.json", _sample(S02_FIXED_HEADERS, S02_FIXED_BODY)),
        ("S03_clean.fixed.json", _sample(S03_FIXED_HEADERS, S03_FIXED_BODY)),
        ("S04_suspect_text.fixed.json", _sample(S04_FIXED_HEADERS, S04_FIXED_BODY)),
    ]
    for filename, payload in fixed_layout:
        destination = fixed / filename
        _write_json(destination, payload)
        written.append(destination)

    fixed_manifest = {
        "manifest_version": 1,
        "samples": [
            {"sample_id": "S01", "sample_file": "S01_server_version.fixed.json", "declaration": "included"},
            {"sample_id": "S02", "sample_file": "S02_plaintext_credential.fixed.json", "declaration": "included"},
            {"sample_id": "S03", "sample_file": "S03_clean.fixed.json", "declaration": "included"},
            {"sample_id": "S04", "sample_file": "S04_suspect_text.fixed.json", "declaration": "included"},
        ],
    }
    fixed_manifest_path = fixed / "manifest.json"
    _write_json(fixed_manifest_path, fixed_manifest)
    written.append(fixed_manifest_path)

    manifest = {
        "manifest_version": 1,
        "samples": [
            {"sample_id": "S01", "sample_file": f"{SAMPLES_DIR}/S01_server_version.json", "declaration": "included"},
            {"sample_id": "S02", "sample_file": f"{SAMPLES_DIR}/S02_plaintext_credential.json", "declaration": "included"},
            {"sample_id": "S03", "sample_file": f"{SAMPLES_DIR}/S03_clean.json", "declaration": "included"},
            {"sample_id": "S04", "sample_file": f"{SAMPLES_DIR}/S04_suspect_text.json", "declaration": "included"},
            {"sample_id": "S05", "sample_file": f"{SAMPLES_DIR}/S05_corrupt.json", "declaration": "included"},
            {"sample_id": "S06", "sample_file": f"{SAMPLES_DIR}/S06_missing.json", "declaration": "included"},
            {"sample_id": "S07", "sample_file": f"{SAMPLES_DIR}/S07_excluded.json", "declaration": "excluded"},
        ],
    }
    manifest_path = target / "manifest.json"
    _write_json(manifest_path, manifest)
    written.append(manifest_path)
    return written
