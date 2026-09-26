import json
import logging
import sys
from datetime import UTC, datetime

from app.core.context import get_correlation_id

_REDACT_KEYS = {"authorization", "api_key", "secret", "password", "token", "signature", "key_secret"}


def redact(obj):
    """Recursively redact secret-looking keys before anything is logged."""
    if isinstance(obj, dict):
        return {k: ("***" if any(s in k.lower() for s in _REDACT_KEYS) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel("WARNING")


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})
