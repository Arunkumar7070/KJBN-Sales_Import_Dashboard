"""Structured JSON logging with a per-request correlation ID.

Every log line is one JSON object with timestamp, level, logger, message,
request_id and path, plus any extra fields passed with `extra=` (for example
method, status_code and duration_ms on the request log). The request ID and
path come from context variables set by the request middleware, so every log
written while handling a request carries them without being passed around.
Request and file bodies are never logged.
"""

import json
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

REQUEST_ID_HEADER = "X-Request-ID"
# Client-supplied IDs are reused only if they look like an ID; anything else
# (too long, spaces, newlines) is replaced so it can't pollute the logs.
VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_request_path: ContextVar[str | None] = ContextVar("request_path", default=None)

# Fields a LogRecord always has; anything else on a record came from `extra=`.
# uvicorn's color_message (terminal colour codes) is dropped as noise.
_STANDARD_RECORD_FIELDS = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "color_message",
}


def request_id_from_header(value: str | None) -> str:
    if value is not None and VALID_REQUEST_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_context(request_id: str | None, path: str | None = None) -> Iterator[None]:
    """Attach request_id (and path) to every log written inside this block."""
    id_token = _request_id.set(request_id)
    path_token = _request_path.set(path)
    try:
        yield
    finally:
        _request_id.reset(id_token)
        _request_path.reset(path_token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _request_id.get(),
            "path": _request_path.get(),
        }
        entry.update(
            (key, value)
            for key, value in vars(record).items()
            if key not in _STANDARD_RECORD_FIELDS
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Send all logs, including uvicorn's, to stdout as JSON lines. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.set_name("json")

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != "json"]
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # The request middleware logs every request (with request_id and duration),
    # so uvicorn's plain-text access line would only duplicate it.
    logging.getLogger("uvicorn.access").disabled = True
