from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


ROLES = {"platform_admin", "project_admin", "project_operator", "reviewer"}
ADMIN_ROLES = {"platform_admin", "project_admin"}


class EvalError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_json(text: str, *, label: str = "JSON") -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvalError(
            "invalid_json",
            f"{label} 无法解析：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}",
        ) from exc


def redact(value: Any) -> Any:
    """Remove common credential forms before evidence is persisted."""
    bearer = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _secret_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return bearer.sub("Bearer [REDACTED]", value)
    return value


def _secret_key(key: str) -> bool:
    normalised = key.lower().replace("-", "_")
    if normalised.endswith("_env") or normalised.endswith("_ref"):
        return False
    if normalised in {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey"}:
        return True
    return normalised.endswith(("_password", "_secret", "_api_key", "_access_token", "_refresh_token"))


def require_role(role: str, allowed: set[str]) -> None:
    if role not in ROLES:
        raise EvalError("invalid_role", f"未知角色：{role}", status=403)
    if role not in allowed:
        raise EvalError("permission_denied", "当前角色没有执行此操作的权限", status=403)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)
