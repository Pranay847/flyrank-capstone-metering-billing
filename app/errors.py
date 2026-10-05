"""One error shape for every non-2xx response, so machines and humans can both read it:

    {"error": {"code": "...", "message": "...", ...details}}
"""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details: dict | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.details}}
