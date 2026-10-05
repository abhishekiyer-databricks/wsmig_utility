"""
Structured logging for the workspace migration utility (PLAN 16.1 Part A).

Every step logs a START line before the work and an END line after it, so at any point in any stage
(inventory, export, import) the output says what the run is doing NOW and how each step ended. A
stuck run's last line names the object and API call in flight; a failed run names the object, the
raw server error and the traceback.

TWO OUTPUTS, NO FILES (verified live on classic DBR 15.4, PLAN 16.1 §3.0):

  ┌────────────────┬───────────────────────────┬────────────────────────────────────────────────┐
  │ output         │ level                     │ content                                        │
  ├────────────────┼───────────────────────────┼────────────────────────────────────────────────┤
  │ notebook cell  │ `log_level` widget        │ stage + phase start/end, a progress line every │
  │ (stdout)       │ (default INFO)            │ PROGRESS_EVERY objects, every WARNING/ERROR —   │
  │                │                           │ capped at CELL_FAILURE_CAP per stage           │
  │ driver log     │ always DEBUG              │ EVERYTHING: the cell lines + per-object start/ │
  │ (sys.__stderr__)│                          │ end lines, every API call, tracebacks, no cap  │
  └────────────────┴───────────────────────────┴────────────────────────────────────────────────┘

  Why: a job's cell output is capped at 30 MB total and EXCEEDING IT FAILS THE RUN, so the cell
  must stay bounded at any scale. Writes to `sys.__stderr__` do not count toward that limit and are
  kept in full in the driver log's `stderr` file, downloadable from the run's Compute → Driver logs
  for 30 days. That file is the one a customer sends us: it holds the cell lines too. Databricks
  writes each stderr line into it TWICE (platform behaviour), so every line carries a process-wide
  sequence number `#n` — duplicates are then obvious and trivially de-duplicated.

  Databricks does not redact secrets in driver logs, so both outputs run the redaction filter.

Call-site API (unchanged from `main`): `log = get_logger(name)` then
`log.info/warning/error/debug(msg, **fields)` → `<msg>  k=v  k=v`. `exc_info=True` (or
`log.exception(...)` inside an `except`) attaches the traceback. There is deliberately NO per-logger
threshold: the level is decided per OUTPUT by `configure_logging`, never by the call site.
"""
from __future__ import annotations

import itertools
import logging
import sys
import time
from contextlib import contextmanager
from typing import Optional

APP_LOGGER_NAME = "wsmig"

# Emit a progress line every N objects in a phase (plus one at the end of the phase).
PROGRESS_EVERY = 500

# At most this many WARNING/ERROR lines reach the CELL per stage; the rest go to the driver log only.
# 2,000 lines ≈ 0.5 MB — far below the 30 MB cell limit even with every other line added.
CELL_FAILURE_CAP = 2000

_FORMAT = ("%(asctime)s #%(seq)d %(levelname)-5s [%(component)s] run=%(run_id)s "
           "stage=%(stage)s | %(message)s")
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_CELL_HANDLER_NAME = "wsmig_cell"
_DRIVER_HANDLER_NAME = "wsmig_driver"

# Per-run context stamped on every record. Defaults keep the columns aligned before configure.
_CONTEXT: dict = {"run_id": "-", "stage": "-"}

# Registered secret values scrubbed from every line (client secrets, tokens).
_SECRETS: set = set()

# Process-wide record counter. `next()` on itertools.count is atomic under the GIL, so worker
# threads can log without a lock and still get unique, increasing numbers.
_SEQ = itertools.count(1)

# Where the full log lives — printed once per stage so an operator never has to ask.
FULL_LOG_HINT = ("Full DEBUG log: this run → Compute → Driver logs → Standard error "
                 "(kept 30 days).")

# The status workbook that lists every outcome, per stage — named in the cell-cap notice.
_STAGE_WORKBOOK = {"INVENTORY": "inventory.xlsx", "EXPORT": "export_status.xlsx",
                   "IMPORT": "import_status.xlsx"}


def _level(level: object, default: int = logging.INFO) -> int:
    """A level name/number → a `logging` level int. Blank/unknown → `default`."""
    if isinstance(level, int):
        return level
    name = str(level or "").strip().upper()
    if not name:
        return default
    resolved = logging.getLevelName(name)
    return resolved if isinstance(resolved, int) else default


# ─────────────────────────── filters ───────────────────────────────────────

class _ContextFilter(logging.Filter):
    """Stamp `run_id`, `stage`, the short `component` name and the shared `seq` on every record.

    Runs as a HANDLER filter (a filter on the `wsmig` logger would not see records logged by child
    loggers). `seq` is assigned only once per record, so the same record carries the same `#n` in
    the cell and in the driver log."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not hasattr(record, "seq"):
            record.seq = next(_SEQ)
            record.run_id = _CONTEXT.get("run_id", "-")
            record.stage = _CONTEXT.get("stage", "-")
            name = record.name
            prefix = APP_LOGGER_NAME + "."
            record.component = name[len(prefix):] if name.startswith(prefix) else name
        return True


class _RedactFilter(logging.Filter):
    """Scrub every registered secret from the rendered message (and any traceback text)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not _SECRETS or getattr(record, "_wsmig_redacted", False):
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad %-format must never drop a log line
            return True
        redacted = _scrub(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        if record.exc_info and not record.exc_text:
            # Render the traceback now so it can be scrubbed too (an exception message can quote a
            # URL or a header). logging.Formatter reuses exc_text when it is set.
            record.exc_text = _scrub(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = _scrub(record.exc_text)
        record._wsmig_redacted = True
        return True


def _scrub(text: str) -> str:
    for secret in _SECRETS:
        if secret and secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


def register_secret(value: Optional[str]) -> None:
    """Register a secret value to be scrubbed from all subsequent log lines (both outputs).

    Only non-trivial values are tracked — a 1-3 char "secret" would redact ordinary words."""
    if value and len(str(value)) >= 4:
        _SECRETS.add(str(value))


def set_context(*, run_id: Optional[str] = None, stage: Optional[str] = None) -> None:
    """Update the run_id / stage stamped on every subsequent line."""
    if run_id is not None and str(run_id).strip():
        _CONTEXT["run_id"] = str(run_id).strip()
    if stage is not None and str(stage).strip():
        _CONTEXT["stage"] = str(stage).strip().upper()


# ─────────────────────────── handlers ──────────────────────────────────────

class _LiveStdoutHandler(logging.Handler):
    """Writes each record to the CURRENT `sys.stdout` at emit time (or a fixed test stream).

    Databricks swaps `sys.stdout` per command cell, so a `StreamHandler(sys.stdout)` would bind the
    stdout of the cell that configured logging and its lines would never surface in later cells.
    Resolving `sys.stdout` per record makes every line land in the cell actually running."""

    def __init__(self, stream=None) -> None:
        super().__init__()
        self._fixed = stream

    def _stream(self):
        return self._fixed if self._fixed is not None else sys.stdout

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._stream()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 — logging must never crash the run
            self.handleError(record)


class _CellHandler(_LiveStdoutHandler):
    """The notebook-cell output: level = `log_level`, plus the per-stage WARNING/ERROR cap.

    After CELL_FAILURE_CAP warning/error lines in a stage, ONE notice line says where the rest are
    and further warning/error lines are kept out of the cell (the driver log still gets all of
    them). INFO lines — phases and progress, which carry the running failure count — keep flowing,
    so the cell always shows where the run is. `live_run` / `configure_logging` reset the cap."""

    def __init__(self, stream=None, cap: int = CELL_FAILURE_CAP) -> None:
        super().__init__(stream)
        self.cap = cap
        self.reset_cap()

    def reset_cap(self) -> None:
        self.failure_lines = 0
        self.suppressed = 0
        self._notified = False

    def handle(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            if self.failure_lines >= self.cap:
                self.suppressed += 1
                if not self._notified:
                    self._notified = True
                    super().handle(self._cap_notice(record))
                return False
            self.failure_lines += 1
        return super().handle(record)

    def _cap_notice(self, record: logging.LogRecord) -> logging.LogRecord:
        stage = _CONTEXT.get("stage", "-")
        workbook = _STAGE_WORKBOOK.get(stage, "the stage's status workbook")
        notice = logging.LogRecord(
            name=record.name, level=logging.WARNING, pathname=record.pathname,
            lineno=record.lineno, args=(), exc_info=None,
            msg=(f"… {self.cap:,} warning/error lines shown for this stage; further failures are "
                 f"in the driver log (stderr) and {workbook}. Progress lines below keep the "
                 f"running failure count."))
        return notice


def _make_handler(handler: logging.Handler, name: str, level: int) -> logging.Handler:
    handler.set_name(name)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(_ContextFilter())
    handler.addFilter(_RedactFilter())
    return handler


def _install_handlers(app: logging.Logger, cell_level: int, cell_stream=None,
                      driver_stream=None) -> None:
    for h in list(app.handlers):
        if h.get_name() in (_CELL_HANDLER_NAME, _DRIVER_HANDLER_NAME):
            app.removeHandler(h)
    app.setLevel(logging.DEBUG)
    # In Databricks notebooks the root logger already has a handler; without this every line would
    # print twice.
    app.propagate = False
    # The driver handler is added FIRST so that, for every record, the context filter (and so the
    # `#seq`) runs on the handler that always accepts it.
    driver = logging.StreamHandler(driver_stream if driver_stream is not None else sys.__stderr__)
    app.addHandler(_make_handler(driver, _DRIVER_HANDLER_NAME, logging.DEBUG))
    app.addHandler(_make_handler(_CellHandler(cell_stream), _CELL_HANDLER_NAME, cell_level))


def _app_logger() -> logging.Logger:
    """The `wsmig` app logger, lazily given the two default handlers (cell INFO, driver DEBUG).

    So a caller that never ran `configure_logging` (a test, a live harness, 00_Install_Jobs) still
    gets the same two outputs."""
    app = logging.getLogger(APP_LOGGER_NAME)
    if not any(h.get_name() == _CELL_HANDLER_NAME for h in app.handlers):
        _install_handlers(app, logging.INFO)
    return app


def _cell_handler() -> Optional[_CellHandler]:
    for h in logging.getLogger(APP_LOGGER_NAME).handlers:
        if h.get_name() == _CELL_HANDLER_NAME and isinstance(h, _CellHandler):
            return h
    return None


def configure_logging(*, run_id: str = "", stage: str = "", level: object = "INFO",
                      cell_stream=None, driver_stream=None) -> logging.Logger:
    """Configure the two outputs for this stage. Idempotent (replaces only its own handlers).

    `level` sets the CELL only (the `log_level` widget; blank → INFO). The driver log is always
    DEBUG. `cell_stream` / `driver_stream` exist for tests; in a notebook the cell resolves
    `sys.stdout` per record and the driver output is `sys.__stderr__`."""
    set_context(run_id=run_id, stage=stage)
    app = logging.getLogger(APP_LOGGER_NAME)
    _install_handlers(app, _level(level), cell_stream=cell_stream, driver_stream=driver_stream)
    return app


def get_log(name: str) -> logging.Logger:
    """The stdlib child logger for a module, homed under `wsmig` so it shares both outputs."""
    _app_logger()
    n = str(name or "").strip()
    if n == APP_LOGGER_NAME or n.startswith(APP_LOGGER_NAME + "."):
        return logging.getLogger(n)
    if n in ("", "__main__", "__mp_main__"):
        n = "notebook"
    return logging.getLogger(APP_LOGGER_NAME).getChild(n)


# ─────────────────────────── formatting helpers ────────────────────────────

def fmt_elapsed(seconds: float) -> str:
    """`42s`, `3m10s`, `1h02m` — compact, for phase/progress lines."""
    s = max(0, int(round(seconds or 0)))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_counts(counts: dict) -> str:
    """`created 48, unchanged 4,440, failed 12` — keys in the given order."""
    return ", ".join(f"{k.replace('_', ' ')} {v:,}" if isinstance(v, int) else f"{k} {v}"
                     for k, v in counts.items())


# ─────────────────────────── StructuredLogger (call-site API) ──────────────

_LEVELNO = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
            "WARNING": logging.WARNING, "ERROR": logging.ERROR}


class StructuredLogger:
    """A named logger: `info/warning/error/debug(msg, **fields)` → one line `msg  k=v  k=v`.

    A thin wrapper over the `wsmig.<name>` stdlib logger. It applies NO threshold of its own — the
    level is decided per output by `configure_logging` (the cell by `log_level`, the driver log
    always DEBUG)."""

    def __init__(self, name: str, level: str = "INFO") -> None:   # `level` kept for API compat
        self.name = name
        self._log = get_log(name)

    def _emit(self, levelno: int, msg: str, exc_info=None, **fields) -> None:
        extra = "  ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{msg}  {extra}" if extra else str(msg)
        # stacklevel=3 → the record's pathname/lineno is the CALLER of info()/warning()/…, not
        # this wrapper.
        self._log.log(levelno, line, exc_info=exc_info, stacklevel=3)

    def debug(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit(logging.DEBUG, msg, exc_info=exc_info or exc, **fields)

    def info(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit(logging.INFO, msg, exc_info=exc_info or exc, **fields)

    def warning(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit(logging.WARNING, msg, exc_info=exc_info or exc, **fields)

    def error(self, msg: str, exc_info=None, exc=None, **fields) -> None:
        self._emit(logging.ERROR, msg, exc_info=exc_info or exc, **fields)

    def exception(self, msg: str, **fields) -> None:
        """ERROR with the active exception's traceback (call inside an `except` block)."""
        self._emit(logging.ERROR, msg, exc_info=True, **fields)

    def log(self, level: str, msg: str, **fields) -> None:
        """Log at a level chosen at run time (`"DEBUG"`/`"INFO"`/`"WARNING"`/`"ERROR"`)."""
        self._emit(_LEVELNO.get(str(level).upper(), logging.INFO), msg, **fields)

    def progress(self, phase: str, done: int, total: int = 0, started: Optional[float] = None,
                 **counts) -> bool:
        """Emit `<phase>: done/total — k n, … (elapsed)` at INFO every PROGRESS_EVERY items and at
        the last one. Returns whether a line was emitted. `total=0` = unknown (every N only).
        No timers, no threads — it is called from the loop that does the work."""
        if done <= 0:
            return False
        if not (done % PROGRESS_EVERY == 0 or (total and done == total)):
            return False
        where = f"{done:,}/{total:,}" if total else f"{done:,}"
        parts = [f"{phase}: {where}"]
        if counts:
            parts.append(f" — {fmt_counts(counts)}")
        if started is not None:
            parts.append(f" ({fmt_elapsed(time.time() - started)})")
        self._log.info("".join(parts), stacklevel=2)
        return True


def get_logger(name: str, level: str = "INFO") -> StructuredLogger:
    """Return a StructuredLogger for `name`."""
    return StructuredLogger(name, level)


def progress(phase: str, done: int, total: int = 0, started: Optional[float] = None,
             **counts) -> bool:
    """Module-level `StructuredLogger.progress` for code without its own logger."""
    return get_logger("progress").progress(phase, done, total, started, **counts)


# ─────────────────────────── stage wrapper ─────────────────────────────────

@contextmanager
def live_run(stage: str):
    """Wrap a stage's main work call: `Stage <X> started` … `Stage <X> finished (status, elapsed)`.

    On an exception: logs it at ERROR with the traceback, then re-raises (the job must still fail).
    Resets the per-stage cell cap. Starts no thread."""
    stage_name = str(stage or "").strip().upper() or _CONTEXT.get("stage", "-")
    set_context(stage=stage_name)
    cell = _cell_handler()
    if cell is not None:
        cell.reset_cap()
    log = get_logger("stage")
    t0 = time.time()
    log.info(f"Stage {stage_name} started")
    try:
        yield
    except BaseException as exc:
        log.error(f"Stage {stage_name} FAILED after {fmt_elapsed(time.time() - t0)}: "
                  f"{type(exc).__name__}: {exc}", exc_info=True)
        raise
    suppressed = cell.suppressed if cell is not None else 0
    tail = (f"; {suppressed:,} further warning/error lines are in the driver log only"
            if suppressed else "")
    log.info(f"Stage {stage_name} finished (status=ok, {fmt_elapsed(time.time() - t0)}){tail}")
