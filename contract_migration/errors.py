"""Stable integer error codes for parameter and state failures."""

from __future__ import annotations


ERROR_CODES = {
    "INVALID_SEED": 1001,
    "INVALID_WINDOW_SIZE": 1002,
    "INVALID_BATCH_CAPACITY": 1003,
    "INVALID_THRESHOLD": 1004,
    "UNKNOWN_CONTRACT": 1005,
    "ROUTE_GAP": 1006,
    "OVERLAPPING_ROUTE": 1007,
    "CAPACITY_EXCEEDED": 1008,
    "INVALID_REQUEST_OP": 1009,
    "SNAPSHOT_CORRUPT": 1010,
    "RESUME_PLAN_MISMATCH": 1011,
    "TARGET_CAPACITY_INSUFFICIENT": 1012,
}


class ContractMigrationError(Exception):
    """An expected, user-visible failure carrying a stable integer code."""

    def __init__(self, symbol: str, message: str):
        if symbol not in ERROR_CODES:
            raise ValueError(f"unknown error symbol: {symbol}")
        self.symbol = symbol
        self.code = ERROR_CODES[symbol]
        self.message = message
        super().__init__(f"{self.code}: {message}")

    def as_dict(self) -> dict:
        return {
            "ok": False,
            "error_code": self.code,
            "symbol": self.symbol,
            "message": self.message,
        }


def error_by_code(code: int) -> ContractMigrationError:
    for symbol, candidate in ERROR_CODES.items():
        if candidate == code:
            return ContractMigrationError(symbol, f"error injected for {symbol}")
    raise ContractMigrationError(
        "INVALID_REQUEST_OP", f"unknown injected integer error code: {code}"
    )
