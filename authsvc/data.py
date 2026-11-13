"""确定性的本地账目样本数据。"""

from __future__ import annotations

ACCOUNTS = (
    {
        "account_id": "ACC-1001",
        "account_name": "Northwind Reserve",
        "owner": "finance-team",
        "currency": "CNY",
        "balance": "184320.50",
        "status": "active",
        "ledger": (
            ("2026-10-01", "credit", "72000.00", "INV-2026-1001"),
            ("2026-10-08", "debit", "12000.00", "INV-2026-1009"),
            ("2026-10-21", "credit", "40000.00", "INV-2026-1017"),
        ),
    },
    {
        "account_id": "ACC-1002",
        "account_name": "Riverside Operating",
        "owner": "ops-team",
        "currency": "CNY",
        "balance": "52880.00",
        "status": "active",
        "ledger": (
            ("2026-10-03", "credit", "18880.00", "INV-2026-1002"),
            ("2026-10-19", "debit", "3200.00", "INV-2026-1021"),
        ),
    },
)

# 对外可见的业务字段集合：检测器以这些字段判定响应是否泄漏账目数据。
BUSINESS_FIELDS = ("account_id", "account_name", "owner", "currency", "balance", "status", "ledger")


def find_account(account_id: str) -> dict | None:
    """按账目 ID 返回一份独立副本，避免调用方修改样本。"""
    normalized = account_id.strip().upper()
    for account in ACCOUNTS:
        if account["account_id"] == normalized:
            return _clone(account)
    return None


def _clone(account: dict) -> dict:
    return {
        "account_id": account["account_id"],
        "account_name": account["account_name"],
        "owner": account["owner"],
        "currency": account["currency"],
        "balance": account["balance"],
        "status": account["status"],
        # 元组在 JSON 中序列化为数组；拷贝一份嵌套结构。
        "ledger": [
            {"date": date, "type": entry_type, "amount": amount, "reference": reference}
            for date, entry_type, amount, reference in account["ledger"]
        ],
    }
