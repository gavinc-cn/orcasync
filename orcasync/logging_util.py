import json
import logging
import logging.handlers
import os
import sys
import time


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


def _make_formatter(fmt: str, datefmt: str) -> logging.Formatter:
    if fmt == "json":
        return _JsonFormatter()
    return _TextFormatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt=datefmt)


def setup_logging(
    level: str = "INFO",
    fmt: str = "text",
    log_file: str | None = None,
    log_backup_count: int = 30,
    role: str | None = None,
    name: str | None = None,
) -> None:
    """Configure the root logger.

    Parameters
    ----------
    level:
        Log level name (DEBUG / INFO / WARNING / ERROR).
    fmt:
        Output format: ``"text"`` (human-readable) or ``"json"``.
    log_file:
        Path for file logging. Supports placeholders expanded at call time:
          - ``{name}`` — replaced with the instance name (if any)
          - ``{role}`` — replaced with *role* (e.g. ``"server"``, ``"client"``)
          - ``{pid}``  — replaced with the current process PID
        If ``{pid}`` is absent it is injected automatically before the
        extension so each process writes to its own file.
        The file rotates at midnight; up to *log_backup_count* copies are kept.
    log_backup_count:
        Number of daily backup log files to retain (default 30).
    role:
        Value for the ``{role}`` placeholder (e.g. the CLI subcommand).
    name:
        Value for the ``{name}`` placeholder (e.g. the ``--name`` CLI argument).
    """
    level_value = getattr(logging, level.upper(), logging.INFO)

    # --- stderr handler (always present) ---
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(_make_formatter(fmt, datefmt="%H:%M:%S"))

    handlers: list[logging.Handler] = [stderr_handler]

    # --- optional file handler ---
    if log_file:
        pid = str(os.getpid())
        ts = time.strftime("%Y%m%d_%H%M%S")

        log_file = log_file.replace("{name}", name or "")
        log_file = log_file.replace("{role}", role or "orcasync")
        log_file = log_file.replace("{ts}", ts)

        # Auto-inject timestamp and PID before the extension when the caller
        # did not place {pid} explicitly, ensuring each process writes to its
        # own uniquely named file that is easy to locate by start time.
        # e.g. "orcasync.log" -> "orcasync.20260517_143022.12345.log"
        if "{pid}" not in log_file:
            base, ext = os.path.splitext(log_file)
            log_file = f"{base}.{ts}.{pid}{ext}"
        else:
            log_file = log_file.replace("{pid}", pid)

        log_dir = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(log_dir, exist_ok=True)

        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        # File logs use the full date so rotated entries are unambiguous
        file_handler.setFormatter(_make_formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers[:] = handlers
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
