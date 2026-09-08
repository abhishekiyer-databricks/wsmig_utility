"""
Structured logger — mirrors the uc-inventory-migration style.

Emits human-readable lines to stdout AND (optionally) structured JSON lines to an
`execution_*.log` file in the run's staging dir. Safe to use before a log file is configured
(stdout-only until `set_log_file` is called).

WHY THE LOCAL-THEN-COPY DANCE (verified live on fvm1 2026-08-03):
  The staging dir is a UC Volume (FUSE). Opening a file there in APPEND mode does not work —
  every `open(path, "a")` after the file exists fails, and because logging must never break the
  pipeline the exception was swallowed, so a whole export produced a ONE-LINE log (only the very
  first record, from the create). Every subsequent record was lost silently.

  So records are appended to a LOCAL /tmp file (where append works normally) and the finished
  file is byte-copied over the Volume copy — the same pattern `ArtifactWriter
  .write_text_local_then_copy` already uses for .xlsx. The mirror happens every
  `_MIRROR_EVERY` records and on `flush_log_file()`, so the Volume copy is never more than a
  few records stale even if the job is killed.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import tempfile
import threading
from typing import Optional

_LOCK = threading.Lock()
_LOG_FILE: Optional[str] = None       # destination on the Volume (or any dest path)
_LOCAL_FILE: Optional[str] = None     # local scratch file we actually append to
_PENDING = 0                          # records written locally but not yet mirrored

# Mirror to the Volume every N records. A log record is ~200 bytes, so a full copy is cheap;
# this bounds "records at risk if the cluster dies" without a Volume write per line.
_MIRROR_EVERY = 25


def set_log_file(path: Optional[str]) -> None:
    """Point all loggers at an execution log file. None = stdout only.

    Starts a fresh local scratch file; the destination is (re)created on the first mirror.
    """
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
        # Never let logging failures break the pipeline. Records stay in the local file and the
        # next mirror retries the whole thing, so nothing is lost unless the cluster dies.
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


class StructuredLogger:
    """A named logger. `info/warning/error(msg, **fields)` prints a line and, if a log file is
    configured, appends one JSON object per call."""

    _LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def __init__(self, name: str, level: str = "INFO") -> None:
        self.name = name
        self._threshold = self._LEVELS.get(level.upper(), 20)

    def _emit(self, level: str, msg: str, **fields) -> None:
        if self._LEVELS.get(level, 20) < self._threshold:
            return
        global _PENDING
        ts = _now()
        # Human line to stdout
        extra = "  ".join(f"{k}={v}" for k, v in fields.items())
        line = f"[{ts}] {level:<7} {self.name}: {msg}"
        if extra:
            line += f"  | {extra}"
        with _LOCK:
            print(line, flush=True)
            if _LOG_FILE and _LOCAL_FILE:
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

    def debug(self, msg: str, **fields) -> None:
        self._emit("DEBUG", msg, **fields)

    def info(self, msg: str, **fields) -> None:
        self._emit("INFO", msg, **fields)

    def warning(self, msg: str, **fields) -> None:
        self._emit("WARNING", msg, **fields)

    def error(self, msg: str, **fields) -> None:
        self._emit("ERROR", msg, **fields)


def get_logger(name: str, level: str = "INFO") -> StructuredLogger:
    """Return a StructuredLogger for `name`."""
    return StructuredLogger(name, level)
