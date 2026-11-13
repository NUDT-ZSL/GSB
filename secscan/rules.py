"""Detection rules: built-in defaults plus optional JSON rule files."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Pattern

from .errors import RulesError

SEVERITY_LABELS: dict[str, str] = {
    "high": "高危",
    "medium": "中危",
    "low": "低危",
}

KNOWN_OPERATORS = ("weak_password", "plaintext_secret", "dangerous_loader")

DEFAULT_WEAK_PASSWORDS: tuple[str, ...] = (
    "123456",
    "password",
    "123456789",
    "12345678",
    "1234567",
    "12345",
    "1234",
    "1234567890",
    "123123",
    "123321",
    "111111",
    "000000",
    "00000000",
    "654321",
    "666666",
    "777777",
    "888888",
    "999999",
    "121212",
    "112233",
    "555555",
    "qwerty",
    "qwerty123",
    "qwertyuiop",
    "1qaz2wsx",
    "1q2w3e4r",
    "abc123",
    "asdfgh",
    "zxcvbnm",
    "admin",
    "admin123",
    "administrator",
    "root",
    "toor",
    "guest",
    "letmein",
    "welcome",
    "monkey",
    "dragon",
    "iloveyou",
    "sunshine",
    "princess",
    "football",
    "baseball",
    "master",
    "superman",
    "trustno1",
    "passw0rd",
    "password1",
    "p@ssw0rd",
    "changeme",
    "default",
    "test",
    "demo",
)

DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "weak_password": "检测到密码类字段使用了常见弱口令，易遭字典攻击或暴力破解。",
    "plaintext_secret": "检测到敏感字段以明文存储，未加密或未使用密钥托管，存在凭据泄露风险。",
    "dangerous_loader": (
        "检测到危险反序列化设置：pickle.loads 或未指定 SafeLoader 的 yaml.load "
        "可能导致任意代码执行。"
    ),
}

DEFAULT_RULES_DATA: dict[str, Any] = {
    "schema_version": 1,
    "name": "builtin-default",
    "description": "内置默认规则：弱口令、明文敏感值、危险反序列化",
    "rules": [
        {
            "id": "SEC-WEAK-PASSWORD-001",
            "type": "weak_password",
            "severity": "high",
            "operator": "weak_password",
            "field_pattern": (
                "^(password|passwd|pwd|pass|passphrase|db_password|db_passwd|"
                "db_pwd|admin_password|root_password|user_password|login_password)$"
            ),
            "description": (
                "检测到密码类字段使用了常见弱口令（如 123456），"
                "易遭字典攻击或暴力破解。"
            ),
        },
        {
            "id": "SEC-PLAINTEXT-SECRET-001",
            "type": "plaintext_secret",
            "severity": "medium",
            "operator": "plaintext_secret",
            "field_pattern": (
                "^(secret|client_secret|app_secret|api_secret|secret_key|"
                "secret_value|token|access_token|refresh_token|auth_token|"
                "bearer_token|api_key|apikey|access_key|private_key|signing_key)$"
            ),
            "description": (
                "检测到敏感字段以明文存储，未加密或未使用密钥托管，"
                "存在凭据泄露风险。"
            ),
        },
        {
            "id": "SEC-DANGEROUS-DESERIALIZATION-001",
            "type": "dangerous_deserialization",
            "severity": "high",
            "operator": "dangerous_loader",
            "field_pattern": (
                "^(loader|load_method|load_func|deserializer|deserialization|"
                "serializer|unpickler|yaml_loader|pickle_loader)$"
            ),
            "match_value_anywhere": True,
            "description": (
                "检测到危险反序列化设置：pickle.loads 或未指定 SafeLoader 的 "
                "yaml.load 可能导致任意代码执行。"
            ),
        },
    ],
}


def normalize_weak_passwords(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_WEAK_PASSWORDS
    if not isinstance(value, list) or not all(
        isinstance(item, (str, int)) and not isinstance(item, bool) for item in value
    ):
        raise RulesError(
            "[ERROR] 规则字段 weak_passwords 必须是字符串/整数数组 "
            "(weak_passwords must be an array of strings or integers)"
        )
    return DEFAULT_WEAK_PASSWORDS + tuple(str(item).strip().lower() for item in value)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    rule_type: str
    severity: str
    operator: str
    description: str
    field_pattern: Optional[Pattern[str]] = None
    field_names: tuple[str, ...] = ()
    weak_passwords: tuple[str, ...] = field(default=DEFAULT_WEAK_PASSWORDS)
    match_value_anywhere: bool = False

    @property
    def severity_label(self) -> str:
        return SEVERITY_LABELS[self.severity]

    @property
    def fixable(self) -> bool:
        return self.operator in ("weak_password", "plaintext_secret")

    def matches_key(self, key: str) -> bool:
        lowered = key.lower()
        if self.field_names and lowered in self.field_names:
            return True
        if self.field_pattern is not None and (
            self.field_pattern.search(key)
            or self.field_pattern.search(lowered)
        ):
            return True
        return False

    def to_public_dict(self) -> dict[str, Any]:
        """Rule definition for future tooling / rule-file round-tripping."""

        return {
            "id": self.rule_id,
            "type": self.rule_type,
            "severity": self.severity,
            "severity_label": self.severity_label,
            "operator": self.operator,
            "description": self.description,
            "field_pattern": self.field_pattern.pattern if self.field_pattern else None,
            "field_names": list(self.field_names),
            "match_value_anywhere": self.match_value_anywhere,
            "fixable": self.fixable,
        }


@dataclass(frozen=True)
class RulesLoadResult:
    rules: tuple[Rule, ...]
    source: str
    name: str
    path: Optional[str]
    warnings: tuple[str, ...]


def _compile_rule(raw: Any, index: int) -> Rule:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise RulesError(f"[ERROR] {where} 必须是 JSON 对象 (rule must be an object)")

    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RulesError(f"[ERROR] {where}.id 必须是非空字符串 (missing rule id)")
    rule_id = rule_id.strip()

    rule_type = raw.get("type")
    if not isinstance(rule_type, str) or not rule_type.strip():
        raise RulesError(
            f"[ERROR] {where}.type 必须是非空字符串 (missing rule type): {rule_id}"
        )

    severity = raw.get("severity")
    if severity not in SEVERITY_LABELS:
        raise RulesError(
            f"[ERROR] {where}.severity 必须是 high/medium/low 之一 "
            f"(invalid severity): {rule_id}: {severity!r}"
        )

    operator = raw.get("operator")
    if operator not in KNOWN_OPERATORS:
        raise RulesError(
            f"[ERROR] {where}.operator 必须是 {', '.join(KNOWN_OPERATORS)} 之一 "
            f"(unknown operator): {rule_id}: {operator!r}"
        )

    field_pattern = raw.get("field_pattern")
    compiled: Optional[Pattern[str]] = None
    if field_pattern is not None:
        if not isinstance(field_pattern, str) or not field_pattern:
            raise RulesError(
                f"[ERROR] {where}.field_pattern 必须是非空字符串 "
                f"(invalid field_pattern): {rule_id}"
            )
        try:
            compiled = re.compile(field_pattern, re.IGNORECASE)
        except re.error as exc:
            raise RulesError(
                f"[ERROR] {where}.field_pattern 不是合法正则表达式 "
                f"(invalid regex): {rule_id}: {exc}"
            ) from exc

    field_names_raw = raw.get("field_names", [])
    if not isinstance(field_names_raw, list) or not all(
        isinstance(item, str) and item for item in field_names_raw
    ):
        raise RulesError(
            f"[ERROR] {where}.field_names 必须是字符串数组 "
            f"(invalid field_names): {rule_id}"
        )
    field_names = tuple(item.strip().lower() for item in field_names_raw)

    if compiled is None and not field_names:
        raise RulesError(
            f"[ERROR] {where} 至少需要 field_pattern 或 field_names "
            f"(a rule needs field_pattern or field_names): {rule_id}"
        )

    description = raw.get("description")
    if description is None:
        description = DEFAULT_DESCRIPTIONS[operator]
    if not isinstance(description, str) or not description.strip():
        raise RulesError(
            f"[ERROR] {where}.description 必须是非空字符串 "
            f"(invalid description): {rule_id}"
        )

    weak_passwords = DEFAULT_WEAK_PASSWORDS
    if "weak_passwords" in raw:
        weak_passwords = normalize_weak_passwords(raw["weak_passwords"])

    match_value_anywhere = raw.get("match_value_anywhere", False)
    if not isinstance(match_value_anywhere, bool):
        raise RulesError(
            f"[ERROR] {where}.match_value_anywhere 必须是布尔值 "
            f"(invalid match_value_anywhere): {rule_id}"
        )

    return Rule(
        rule_id=rule_id,
        rule_type=rule_type.strip(),
        severity=severity,
        operator=operator,
        description=description.strip(),
        field_pattern=compiled,
        field_names=field_names,
        weak_passwords=weak_passwords,
        match_value_anywhere=match_value_anywhere,
    )


def builtin_rules() -> tuple[Rule, ...]:
    return tuple(
        _compile_rule(raw, index)
        for index, raw in enumerate(DEFAULT_RULES_DATA["rules"])
    )


def _parse_rule_document(document: Any) -> tuple[list[Any], str, str]:
    if isinstance(document, list):
        return document, "custom", "custom-rules"
    if not isinstance(document, dict):
        raise RulesError(
            "[ERROR] 规则文件顶层必须是 JSON 对象或数组 "
            "(rules file root must be an object or array)"
        )

    rules_raw = document.get("rules")
    if not isinstance(rules_raw, list):
        raise RulesError(
            "[ERROR] 规则文件缺少 rules 数组 (rules file has no 'rules' array)"
        )

    name = document.get("name", "custom-rules")
    if not isinstance(name, str) or not name.strip():
        raise RulesError("[ERROR] 规则文件 name 必须是非空字符串 (invalid rules name)")

    schema_version = document.get("schema_version", 1)
    if schema_version != 1:
        raise RulesError(
            f"[ERROR] 不支持的规则文件 schema_version (unsupported schema_version): "
            f"{schema_version!r}"
        )

    return rules_raw, "file", name.strip()


def load_rules(path: Optional[str]) -> RulesLoadResult:
    """Load rules from *path*.

    A missing rules file is a warning, not a failure: the built-in defaults
    are used and scanning continues. A file that exists but is malformed is a
    hard error so the operator can fix the policy file.
    """

    if path is None:
        return RulesLoadResult(
            rules=builtin_rules(),
            source="builtin-default",
            name=DEFAULT_RULES_DATA["name"],
            path=None,
            warnings=(),
        )

    p = Path(path)
    if not p.exists():
        warning = (
            f"[WARN] 规则文件不存在，使用内置默认规则继续扫描 "
            f"(rules file not found; using built-in default rules): {path}"
        )
        return RulesLoadResult(
            rules=builtin_rules(),
            source="builtin-default",
            name=DEFAULT_RULES_DATA["name"],
            path=None,
            warnings=(warning,),
        )
    if p.is_dir():
        raise RulesError(
            f"[ERROR] 规则文件路径是目录，而不是文件 "
            f"(rules path is a directory): {path}"
        )

    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RulesError(
            f"[ERROR] 规则文件不是 UTF-8 文本 (rules file is not valid UTF-8): "
            f"{path}: {exc}"
        ) from exc
    except OSError as exc:
        raise RulesError(
            f"[ERROR] 无法读取规则文件 (cannot read rules file): {path}: {exc}"
        ) from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RulesError(
            f"[ERROR] 规则文件格式非法（JSON 解析失败, invalid rules JSON）: "
            f"{path}: line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc

    rules_raw, source, name = _parse_rule_document(document)
    if not rules_raw:
        raise RulesError(
            f"[ERROR] 规则文件 rules 数组为空 (rules array is empty): {path}"
        )

    compiled: list[Rule] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(rules_raw):
        rule = _compile_rule(raw, index)
        if rule.rule_id in seen_ids:
            raise RulesError(
                f"[ERROR] 规则 id 重复 (duplicate rule id): {rule.rule_id}"
            )
        seen_ids.add(rule.rule_id)
        compiled.append(rule)

    return RulesLoadResult(
        rules=tuple(compiled),
        source=source,
        name=name,
        path=str(p),
        warnings=(),
    )
