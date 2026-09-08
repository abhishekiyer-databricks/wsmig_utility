"""Minimal test runner — pytest is not installable in this environment (no package index).

The test modules are deliberately written as plain `test_*()` functions with any fixture-shaped
arguments defaulted (`tmp_path=None`, `monkeypatch=None`), so they need no framework: this runner
imports each module, calls every `test_*` callable that takes no REQUIRED argument, and reports
pass/fail with the traceback. Live harnesses (`live_*`, `fixtures_*`) are excluded — they need a
workspace and are run explicitly.

Run: python3 -m tests.run_tests [module_substring ...]
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
import traceback

# Modules that hit a real workspace — never auto-run.
_EXCLUDE_PREFIXES = ("live_", "fixtures_", "run_against_", "run_tests", "fakes", "_")


def _test_modules() -> list[str]:
    import tests
    out = []
    for mod in pkgutil.iter_modules(tests.__path__):
        name = mod.name
        if name.startswith(_EXCLUDE_PREFIXES):
            continue
        out.append(f"tests.{name}")
    return sorted(out)


def _callable_without_required_args(fn) -> bool:
    """True if `fn` can be called with no arguments (fixture-shaped params must be defaulted)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return all(p.default is not inspect.Parameter.empty
               or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
               for p in sig.parameters.values())


def main(argv: list[str]) -> int:
    filters = argv[1:]
    passed, failed = 0, []
    for mod_name in _test_modules():
        if filters and not any(f in mod_name for f in filters):
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            failed.append((mod_name, "<import>", traceback.format_exc()))
            print(f"!! {mod_name}: IMPORT FAILED")
            continue
        names = [n for n in dir(mod) if n.startswith("test_")]
        for name in sorted(names):
            fn = getattr(mod, name)
            if not callable(fn) or not _callable_without_required_args(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"  ok   {mod_name}::{name}")
            except Exception:
                failed.append((mod_name, name, traceback.format_exc()))
                print(f"  FAIL {mod_name}::{name}")

    print("\n" + "=" * 74)
    for mod_name, name, tb in failed:
        print(f"\nFAIL {mod_name}::{name}\n{tb}")
    print("=" * 74)
    print(f"{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
