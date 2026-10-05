"""Structured JSON logs: one JSON object per line, the format log platforms
(Datadog, CloudWatch, ELK) expect. Sensitive fields are redacted automatically."""
import json
import logging
import sys
from datetime import datetime, timezone

SENSITIVE = {"authorization", "secret", "source_account", "destination_account", "api_key"}
_STANDARD = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = "[REDACTED]" if key in SENSITIVE else value
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
