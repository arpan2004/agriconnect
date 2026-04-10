import re
import threading
import time
from collections import deque
from typing import Any, Dict

from observability import log_audit_event

MAX_INPUT_LENGTH = 200

_INJECTION_PATTERNS = [
    r";|\|\||&&|`|\$\(|\$\{|<\?|--|/\*|\*/|#",
    r"<\s*script|<\s*iframe|<\s*svg|<\s*img",
    r"\{\{|\}\}|\$\{",
    r"ignore\s+previous|system\s+prompt|developer\s+message|act\s+as",
    r"you\s+are\s+chatgpt|do\s+not\s+follow|override\s+instructions",
    r"^\s*(system|assistant|user)\s*:",
    r"<\s*tool\b|<\s*/\s*tool\b|mcp\s*:\s*tool",
    r"curl\s+http|wget\s+http",
    r"\|\s*sh|\|\s*bash",
]

_INJECTION_REGEX = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

_SECRET_PATTERNS = [
    re.compile(r"(api[_-]?key\s*[:=]\s*)([A-Za-z0-9\-_.]{6,})", re.IGNORECASE),
    re.compile(r"(token\s*[:=]\s*)([A-Za-z0-9\-_.]{6,})", re.IGNORECASE),
    re.compile(r"(secret\s*[:=]\s*)([A-Za-z0-9\-_.]{6,})", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*)(\S+)", re.IGNORECASE),
    re.compile(r"(bearer\s+)([A-Za-z0-9\-_.=]{8,})", re.IGNORECASE),
]


class RateLimiter:
    def __init__(self, limit: int = 30, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._timestamps = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def check(self) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._timestamps) >= self.limit:
                return False
            self._timestamps.append(now)
            return True

    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._prune(now)
            current = len(self._timestamps)
        return {
            "limit": self.limit,
            "window_seconds": self.window_seconds,
            "current_count": current,
            "remaining": max(self.limit - current, 0),
        }


def sanitize_input(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if len(cleaned) > MAX_INPUT_LENGTH:
        cleaned = cleaned[:MAX_INPUT_LENGTH]
    scan_target = cleaned[:MAX_INPUT_LENGTH]
    if _INJECTION_REGEX.search(scan_target):
        log_audit_event("INJECTION_DETECTED", {"value": cleaned[:80]})
        raise ValueError("Input failed security validation.")
    return cleaned


def sanitize_output(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = text.replace("\x00", "")
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*(system|assistant|user)\s*:", line, re.IGNORECASE):
            continue
        if re.search(r"<\s*tool\b|<\s*/\s*tool\b|mcp\s*:\s*tool", line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines)


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def _raise_validation_error(message: str, metadata: Dict[str, Any]) -> None:
    log_audit_event("VALIDATION_ERROR", metadata)
    raise ValueError(message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_tool_args(args: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    if schema.get("type") != "object":
        return args
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_allowed = schema.get("additionalProperties", True)

    for key in required:
        if key not in args:
            _raise_validation_error(
                f"Missing required field: {key}",
                {"missing": key, "received": list(args.keys())},
            )

    if not additional_allowed:
        for key in args.keys():
            if key not in properties:
                _raise_validation_error(
                    f"Unexpected field: {key}",
                    {"unexpected": key, "received": list(args.keys())},
                )

    for key, value in args.items():
        if key not in properties:
            continue
        prop = properties[key]
        expected_type = prop.get("type")
        if expected_type == "string":
            if not isinstance(value, str):
                _raise_validation_error(
                    f"Field {key} must be a string.",
                    {"field": key, "type": type(value).__name__},
                )
            min_len = prop.get("minLength")
            max_len = prop.get("maxLength")
            if min_len is not None and len(value) < min_len:
                _raise_validation_error(
                    f"Field {key} must be at least {min_len} characters.",
                    {"field": key},
                )
            if max_len is not None and len(value) > max_len:
                _raise_validation_error(
                    f"Field {key} must be at most {max_len} characters.",
                    {"field": key},
                )
        elif expected_type == "number":
            if not _is_number(value):
                _raise_validation_error(
                    f"Field {key} must be a number.",
                    {"field": key, "type": type(value).__name__},
                )
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if minimum is not None and value < minimum:
                _raise_validation_error(
                    f"Field {key} must be >= {minimum}.",
                    {"field": key},
                )
            if maximum is not None and value > maximum:
                _raise_validation_error(
                    f"Field {key} must be <= {maximum}.",
                    {"field": key},
                )
        elif expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                _raise_validation_error(
                    f"Field {key} must be an integer.",
                    {"field": key, "type": type(value).__name__},
                )
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if minimum is not None and value < minimum:
                _raise_validation_error(
                    f"Field {key} must be >= {minimum}.",
                    {"field": key},
                )
            if maximum is not None and value > maximum:
                _raise_validation_error(
                    f"Field {key} must be <= {maximum}.",
                    {"field": key},
                )
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                _raise_validation_error(
                    f"Field {key} must be a boolean.",
                    {"field": key, "type": type(value).__name__},
                )
        elif expected_type == "array":
            if not isinstance(value, list):
                _raise_validation_error(
                    f"Field {key} must be an array.",
                    {"field": key, "type": type(value).__name__},
                )
        elif expected_type == "object":
            if not isinstance(value, dict):
                _raise_validation_error(
                    f"Field {key} must be an object.",
                    {"field": key, "type": type(value).__name__},
                )

        if "enum" in prop and value not in prop["enum"]:
            _raise_validation_error(
                f"Field {key} must be one of {prop['enum']}",
                {"field": key, "value": value},
            )

    return args


rate_limiter = RateLimiter()
