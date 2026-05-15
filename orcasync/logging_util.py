import json
import logging
import sys


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            out.update(fields)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record):
        base = super().format(record)
        fields = getattr(record, "fields", None)
        if fields:
            kv = " ".join(f"{k}={_fmt_val(v)}" for k, v in fields.items())
            return f"{base} {kv}"
        return base


def _fmt_val(v):
    if v is None:
        return "-"
    if isinstance(v, str) and (" " in v or "=" in v):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def setup_logging(level="INFO", fmt="text"):
    level_value = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _TextFormatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level_value)


def log_event(logger, level, event, **fields):
    """Emit a structured log line.

    The `event` is a short dotted name (e.g. "scan.done", "transfer.applied").
    Extra fields render as `key=value` suffixes in text mode and merge into
    the JSON object in json mode.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    if not logger.isEnabledFor(level):
        return
    logger.log(level, event, extra={"fields": fields})
