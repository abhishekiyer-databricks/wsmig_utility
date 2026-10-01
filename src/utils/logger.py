"""
Structured logging for the workspace migration utility.

PLAN 13 B5 — matches the UC governance-migration utility's shipped ``logging_util.py`` (app logger
renamed ``uc_sync`` → ``wsmig``), so an operator reads ONE logging style across both tools. The
primary sink is the **running notebook cell** (not a file): every step announces itself live, a
stuck run's last line names the step it hung on, and a failed run is diagnosable from the cell
output alone.

Two layers live in this one module:

1. The UC-style stdlib-``logging`` infrastructure — a named app logger ``wsmig`` with per-module
   child loggers via :func:`get_log`, a :class:`_LiveStdoutHandler` that resolves ``sys.stdout``
   **per record** (so lines land in the currently-running cell and stream live), ``run_id``/``stage``
   stamped on every line by :class:`_ContextFilter`, secret scrubbing by :class:`_RedactFilter`, an
   optional ``StringIO`` capture buffer, and :func:`pin_stdout` so B6 worker threads still reach the
   cell. ``configure_logging`` is idempotent and defaults to level ``DEBUG``.

2. The pre-existing :class:`StructuredLogger` / :func:`get_logger` façade (``log.info(msg, **fields)``)
   kept verbatim for the ~65 call sites and the local-then-copy JSON **file mirror** (the optional
   SECONDARY sink described below). Its stdout now flows through the ``wsmig`` app logger (so it gets
   the live cell handler, context stamping, redaction and the capture buffer) instead of a bare
   ``print`` — no double printing, one consistent format.

WHY THE LOCAL-THEN-COPY DANCE for the file mirror (verified live on fvm1 2026-08-03):
  The staging dir is a UC Volume (FUSE). Opening a file there in APPEND mode does not work — every
  ``open(path, "a")`` after the file exists fails, so a whole export produced a ONE-LINE log. Records
  are therefore appended to a LOCAL /tmp file (where append works) and the finished file is
  byte-copied over the Volume copy, mirroring every ``_MIRROR_EVERY`` records and on
  ``flush_log_file()``. This file is now the OPTIONAL secondary artifact; the cell is primary.

Design note: the "full stack trace" benefit comes from ``log.error(msg, exc_info=True)`` /
``log.exception(msg)`` inside an ``except`` block — never a bare ``print(str(exc))``.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from typing import Optional

# ─────────────────────────── UC-style stdlib logging infra (B5) ────────────────────────────

APP_LOGGER_NAME = "wsmig"

# Line format: timestamp, level, logger name, run_id, stage, message — greppable, with run_id +
# stage on every line (matches the UC utility verbatim, modulo the app name).
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] run=%(run_id)s stage=%(stage)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_STREAM_HANDLER_NAME = "wsmig_stream"
_BUFFER_HANDLER_NAME = "wsmig_buffer"

# Mutable per-run context injected into every record by ``_ContextFilter``. Defaults keep the
# columns aligned before ``configure_logging``/``set_context`` runs.
_CONTEXT: dict[str, str] = {"run_id": "-", "stage": "-"}

# Registered secret values scrubbed from every line (client secrets, tokens).
_SECRETS: set[str] = set()

# The active in-memory capture buffer (set by ``configure_logging(capture=True)``).
_BUFFER: Optional[io.StringIO] = None

# When set (by ``pin_stdout``), the live handler writes to THIS stream instead of resolving
# ``sys.stdout`` per record. Databricks can redirect ``sys.stdout`` thread-locally, so a worker
# thread's ``sys.stdout`` may NOT be the cell's — pinning the main thread's cell stream for the
# duration of a parallel section keeps worker lines in the cell (B5 thread-safety / B6).
_PINNED_STREAM = None


def _level(level: object) -> int:
    """Resolve a level name/number to a ``logging`` level int (default DEBUG)."""
    if isinstance(level, int):
        return level
    name = str(level or "DEBUG").strip().upper()
    return getattr(logging, name, logging.DEBUG)


class _LiveStdoutHandler(logging.Handler):
    """Writes each record to the CURRENT ``sys.stdout`` at emit time (or the pinned stream).

    Databricks swaps ``sys.stdout`` per command cell, so a normal ``StreamHandler(sys.stdout)``
    binds the stdout captured when ``configure_logging`` ran — usually an EARLIER cell — and its
    lines never surface in the cell actually executing. Resolving ``sys.stdout`` on every ``emit``
    makes each line land in the running cell and stream live. When a parallel section pins a stream
    (``pin_stdout``), that stream is used so worker threads (whose thread-local ``sys.stdout`` may
    differ) still reach the cell.
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin I/O
        try:
            msg = self.format(record)
            stream = _PINNED_STREAM if _PINNED_STREAM is not None else sys.stdout
            stream.write(msg + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 - never let logging crash the run
            self.handleError(record)


class _ContextFilter(logging.Filter):
    """Inject the current ``run_id`` and ``stage`` onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.run_id = _CONTEXT.get("run_id", "-")
        record.stage = _CONTEXT.get("stage", "-")
        return True


class _RedactFilter(logging.Filter):
    """Scrub any registered secret value from the fully-rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not _SECRETS:
            return True
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive; never drop a log line
            return True
        redacted = msg
        for secret in _SECRETS:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def register_secret(value: Optional[str]) -> None:
    """Register a secret value to be scrubbed from all subsequent log lines.

    Only non-trivial values are tracked (a 1-2 char "secret" would redact everywhere)."""
    if value and len(str(value)) >= 4:
        _SECRETS.add(str(value))


def set_context(*, run_id: Optional[str] = None, stage: Optional[str] = None) -> None:
    """Update the run_id / stage stamped on every subsequent line."""
    if run_id is not None and str(run_id).strip():
        _CONTEXT["run_id"] = str(run_id).strip()
    if stage is not None and str(stage).strip():
        _CONTEXT["stage"] = str(stage).strip()


def _make_handler(handler: logging.Handler, name: str) -> logging.Handler:
    handler.set_name(name)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(_ContextFilter())
    handler.addFilter(_RedactFilter())
    return handler


def _remove_our_handlers(app: logging.Logger) -> None:
    """Remove handlers this module previously attached (idempotent re-configure)."""
    for h in list(app.handlers):
        if h.get_name() in (_STREAM_HANDLER_NAME, _BUFFER_HANDLER_NAME):
            app.removeHandler(h)


def _app_logger() -> logging.Logger:
    """The ``wsmig`` app logger, lazily ensured to have at least the live-stdout handler.

    So ``get_log(...)`` / ``get_logger(...)`` stream to the cell even if a caller (a test, a live
    harness) never called ``configure_logging`` — matching the old ``print``-always behaviour."""
    app = logging.getLogger(APP_LOGGER_NAME)
    if not any(h.get_name() == _STREAM_HANDLER_NAME for h in app.handlers):
        app.setLevel(logging.DEBUG)
        app.propagate = False
        app.addHandler(_make_handler(_LiveStdoutHandler(), _STREAM_HANDLER_NAME))
    return app


def configure_logging(
    *,
    run_id: str = "",
    stage: str = "",
    level: object = "DEBUG",
    capture: bool = True,
    stream=None,
) -> Optional[io.StringIO]:
    """Configure the ``wsmig`` app logger once per run and return the capture buffer.

    Idempotent: re-invoking replaces this module's own handlers (never duplicates them) and never
    touches handlers added elsewhere. Returns the ``StringIO`` capture buffer when ``capture`` is
    true (else ``None``), so the caller can write the run's full log alongside the report on the
    Volume (the optional secondary sink)."""
    global _BUFFER
    app = logging.getLogger(APP_LOGGER_NAME)
    app.setLevel(_level(level))
    # In Databricks notebooks the root logger already has a handler, so without this every line
    # prints twice (B5 gotcha / UC tweak 1).
    app.propagate = False
    set_context(run_id=run_id, stage=stage)

    _remove_our_handlers(app)

    # No explicit stream → the live-stdout handler (resolves sys.stdout per record, so lines land in
    # the currently-running cell). An explicit stream (tests) is a fixed target, so a plain
    # StreamHandler is correct there.
    stdout_handler: logging.Handler = (
        logging.StreamHandler(stream) if stream is not None else _LiveStdoutHandler()
    )
    app.addHandler(_make_handler(stdout_handler, _STREAM_HANDLER_NAME))

    _BUFFER = None
    if capture:
        _BUFFER = io.StringIO()
        app.addHandler(_make_handler(logging.StreamHandler(_BUFFER), _BUFFER_HANDLER_NAME))
    return _BUFFER


def get_log(name: str) -> logging.Logger:
    """Return the child logger for a module (``get_log(__name__)``).

    Names already under ``wsmig`` are returned as-is; ``__main__`` / anything else is re-homed under
    the app logger so notebook code shares the same handlers and format."""
    _app_logger()  # ensure the app logger has a live handler
    n = str(name or "").strip()
    if n == APP_LOGGER_NAME or n.startswith(APP_LOGGER_NAME + "."):
        return logging.getLogger(n)
    if n in ("", "__main__", "__mp_main__"):
        n = "notebook"
    return logging.getLogger(APP_LOGGER_NAME).getChild(n)


def get_log_buffer() -> Optional[io.StringIO]:
    """The active capture buffer, if ``configure_logging(capture=True)`` was called."""
    return _BUFFER


def get_captured_log() -> str:
    """The full captured run log as a string (empty when capture is off)."""
    return _BUFFER.getvalue() if _BUFFER is not None else ""


@contextmanager
def pin_stdout(stream=None):
    """Pin the live handler to a fixed stdout stream for a parallel section (B5 thread-safety / B6).

    Databricks may redirect ``sys.stdout`` thread-locally, so a worker thread's ``sys.stdout`` is not
    necessarily the cell's. Call this ON THE MAIN THREAD before fanning work out to a thread pool:
    it captures the cell's current ``sys.stdout`` (or an explicit ``stream``) and makes every
    ``_LiveStdoutHandler`` emit — from any thread — write to it (the handler lock serialises the
    writes), so worker lines still appear in the cell. Restores the prior pin on exit (re-entrant)."""
    global _PINNED_STREAM
    prev = _PINNED_STREAM
    _PINNED_STREAM = stream if stream is not None else sys.stdout
    try:
        yield _PINNED_STREAM
    finally:
        _PINNED_STREAM = prev


# ─────────────────────────── file mirror (optional secondary sink) ──────────────────────────

_LOCK = threading.Lock()
_LOG_FILE: Optional[str] = None       # destination on the Volume (or any dest path)
_LOCAL_FILE: Optional[str] = None     # local scratch file we actually append to
_PENDING = 0                          # records written locally but not yet mirrored

# Mirror to the Volume every N records. A log record is ~200 bytes, so a full copy is cheap; this
# bounds "records at risk if the cluster dies" without a Volume write per line.
_MIRROR_EVERY = 25


def set_log_file(path: Optional[str]) -> None:
    """Point the file mirror at an execution log file. None = cell/stdout only.

    Starts a fresh local scratch file; the destination is (re)created on the first mirror. This is
    the OPTIONAL secondary sink — the primary sink is always the running notebook cell."""
    global _LOG_FILE, _LOCAL_FILE, _PENDING
    with _LOCK:
        _LOG_FILE = path
        _PENDING = 0
        if not path:
            _LOCAL_FILE = None
            return
        local_dir = tempfile.mkdtemp(prefix="wsmig_log_")
        _LOCAL_FILE = os.path.join(local_dir, os.path.basename(path) or "execution.log")
        try:
            # Truncate/create so a re-run in the same dir starts clean rather than doubling up.
            with open(_LOCAL_FILE, "w", encoding="utf-8"):
                pass
        except Exception:
            _LOCAL_FILE = None


def _mirror_locked() -> None:
    """Copy the local log over the destination. Caller MUST hold _LOCK. Never raises."""
    global _PENDING
    if not (_LOG_FILE and _LOCAL_FILE):
        return
    try:
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        shutil.copyfile(_LOCAL_FILE, _LOG_FILE)
        _PENDING = 0
    except Exception:
        # Never let logging failures break the pipeline. Records stay in the local file and the next
        # mirror retries the whole thing, so nothing is lost unless the cluster dies.
        pass


def flush_log_file() -> None:
    """Force the destination copy up to date. Call at the END of a notebook/run."""
    with _LOCK:
        _mirror_locked()


def log_file_paths() -> tuple[Optional[str], Optional[str]]:
    """(destination, local scratch) — for diagnostics/tests."""
    return _LOG_FILE, _LOCAL_FILE


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ─────────────────────────── StructuredLogger façade (kept API) ─────────────────────────────

_LEVELNO = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
            "WARNING": logging.WARNING, "ERROR": logging.ERROR}


class StructuredLogger:
    """A named logger. ``info/warning/error/debug(msg, **fields)`` streams a line to the cell (via
    the ``wsmig`` app logger: live handler + context + redaction + capture buffer) and, if a log
    file is configured, appends one JSON object per call to the local-then-copy mirror.

    ``exc_info=True`` (or ``exc=<exception>``) on a call attaches the traceback — use it inside an
    ``except`` block instead of a bare ``print(str(exc))`` (B5 #3)."""

    _LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def __init__(self, name: str, level: str = "INFO") -> None:
        self.name = name
        self._threshold = self._LEVELS.get(level.upper(), 20)
        self._log = get_log(name)

    def _emit(self, level: str, msg: str, exc_info=None, **fields) -> None:
        if self._LEVELS.get(level, 20) < self._threshold:
            return
        global _PENDING
        ts = _now()
        extra = "  ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{msg}  | {extra}" if extra else msg
        # Primary sink: the running cell, via the app logger (context/redaction/capture/live handler).
        self._log.log(_LEVELNO.get(level, logging.INFO), line, exc_info=exc_info)
        # Secondary sink: the JSON file mirror (local append → Volume copy), unchanged.
        if _LOG_FILE and _LOCAL_FILE:
            with _LOCK:
                record = {"ts": ts, "level": level, "logger": self.name, "msg": msg, **fields}
                try:
                    # Append to the LOCAL file (append on the Volume itself silently fails).
                    with open(_LOCAL_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, default=str) + "\n")
                    _PENDING += 1
                except Exception:
                    # Never let logging failures break the pipeline.
                    return
                # An ERROR/WARNING is exactly what someone reads after a crash → mirror at once.
                if _PENDING >= _MIRROR_EVERY or level in ("ERROR", "WARNING"):
                    _mirror_locked()

    def debug(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit("DEBUG", msg, exc_info=exc_info or exc, **fields)

    def info(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit("INFO", msg, exc_info=exc_info or exc, **fields)

    def warning(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit("WARNING", msg, exc_info=exc_info or exc, **fields)

    def error(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit("ERROR", msg, exc_info=exc_info or exc, **fields)

    def exception(self, msg: str, **fields) -> None:
        """Log at ERROR with the active exception's traceback (call inside an ``except`` block)."""
        self._emit("ERROR", msg, exc_info=True, **fields)


def get_logger(name: str, level: str = "INFO") -> StructuredLogger:
    """Return a StructuredLogger for `name`."""
    return StructuredLogger(name, level)
