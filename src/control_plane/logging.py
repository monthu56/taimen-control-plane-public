"""JSON structured logging with request-id propagation and secret redaction."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
# ``X-Run-Id`` trace correlator (ADR-0039), logged as ``run_id``. Distinct from
# the execution Run entity, whose identifier is logged as ``runId``/``run``.
trace_run_id_var: ContextVar[str | None] = ContextVar("trace_run_id", default=None)

_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)

_SENSITIVE_KEYS = frozenset({"authorization", "api_key", "apikey", "key_hash", "password", "token"})


def redact(value: object, key: str | None = None) -> object:
    """Recursively redact obviously sensitive values in log extras."""
    if key is not None and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        trace_run_id = trace_run_id_var.get()
        if trace_run_id:
            payload["run_id"] = trace_run_id
        for attr, value in record.__dict__.items():
            if attr not in _STD_ATTRS and not attr.startswith("_"):
                payload[attr] = redact(value, attr)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn loggers propagate into the root JSON handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
