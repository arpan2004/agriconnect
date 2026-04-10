import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_LOG_DIR = os.getenv("LOG_DIR", "/tmp/agriconnect-logs")
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_SERVER_LOG_NAME = "server.log"
_SERVER_JSON_NAME = "server.log.jsonl"
_TRACES_NAME = "traces.jsonl"
_AUDIT_NAME = "audit.jsonl"

_initialized = False
_init_lock = threading.Lock()


def _ensure_log_dir() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)


def _log_path(filename: str) -> str:
    return os.path.join(_LOG_DIR, filename)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        return f"[{ts}] {record.levelname} {record.name} - {record.getMessage()}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=True)


def _init_logging() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        _ensure_log_dir()
        root = logging.getLogger("agriconnect")
        root.setLevel(_LOG_LEVEL)
        root.propagate = False

        if not root.handlers:
            text_handler = logging.FileHandler(_log_path(_SERVER_LOG_NAME))
            text_handler.setFormatter(_TextFormatter())

            json_handler = logging.FileHandler(_log_path(_SERVER_JSON_NAME))
            json_handler.setFormatter(_JsonFormatter())

            root.addHandler(text_handler)
            root.addHandler(json_handler)
        _initialized = True


def get_logger(name: str = "agriconnect") -> logging.Logger:
    _init_logging()
    return logging.getLogger(name)


def _append_json_line(path: str, payload: Dict[str, Any]) -> None:
    _ensure_log_dir()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True))
        handle.write("\n")


class ChildSpan:
    def __init__(self, parent: "ToolCallSpan", name: str, result: Optional[str] = None) -> None:
        self.parent = parent
        self.name = name
        self.result = result
        self._start = None

    def __enter__(self) -> "ChildSpan":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._start is None:
            return
        duration_ms = (time.perf_counter() - self._start) * 1000.0
        self.parent.add_child_span(self.name, duration_ms, self.result)


class ToolCallSpan:
    def __init__(self, tool_name: str, arg_keys: List[str]) -> None:
        self.tool_name = tool_name
        self.arg_keys = list(arg_keys)
        self.request_id = self._build_request_id(tool_name)
        self.start_time = datetime.now(timezone.utc)
        self._start_perf = time.perf_counter()
        self._child_spans: List[Dict[str, Any]] = []

    @staticmethod
    def _build_request_id(tool_name: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        return f"{tool_name}-{stamp}"

    def add_child_span(self, name: str, duration_ms: float, result: Optional[str] = None) -> None:
        payload = {"name": name, "duration_ms": round(duration_ms, 3)}
        if result is not None:
            payload["result"] = result
        self._child_spans.append(payload)

    def child_span(self, name: str, result: Optional[str] = None) -> ChildSpan:
        return ChildSpan(self, name, result)

    def finish(self, outcome: str, error: Optional[str] = None) -> None:
        duration_ms = (time.perf_counter() - self._start_perf) * 1000.0
        payload = {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "start_time": self.start_time.isoformat(),
            "duration_ms": round(duration_ms, 3),
            "outcome": outcome,
            "arg_keys": self.arg_keys,
            "child_spans": self._child_spans,
        }
        if error:
            payload["error"] = error
        _append_json_line(_log_path(_TRACES_NAME), payload)


def log_audit_event(event_type: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    redacted_metadata = metadata or {}
    try:
        from security import redact_secrets
        redacted_metadata = {
            key: redact_secrets(str(value)) for key, value in (metadata or {}).items()
        }
    except Exception:
        redacted_metadata = metadata or {}

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "metadata": redacted_metadata,
    }
    _append_json_line(_log_path(_AUDIT_NAME), payload)
    logger = get_logger("agriconnect.audit")
    logger.info(f"{event_type} {redacted_metadata}")


def trace_span(tool_name: str, arg_keys: List[str]) -> ToolCallSpan:
    return ToolCallSpan(tool_name, arg_keys)


def log_exception(message: str) -> None:
    logger = get_logger("agriconnect")
    logger.error(message)
