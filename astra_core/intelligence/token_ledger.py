"""token_ledger.py — per-call token accounting for every real LLM completion.

The API already reports usage on every call; the gateway used to discard it,
so any statement about ASTRA's token spend was an estimate. This module is
the persistence side of the fix: ``record_usage`` appends one JSONL line per
tagged call, and ``daily_totals`` reads the running totals back.

Ledger path: ``$ASTRA_TOKEN_LEDGER`` or
``~/.astra_persistent/evolved_programs/token_ledger.jsonl``.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

_DEFAULT_DIR = Path.home() / ".astra_persistent" / "evolved_programs"
LEDGER_NAME = "token_ledger.jsonl"
# Rotation cap (2026-10-10): the ledger is append-only and now records every
# call again (proposer + judge + meta-prompt), so it gets the same rotation
# policy as the verdict log — an autonomous loop must never fill the disk
# through accounting. Readers aggregate per day; the .1 file is kept for audit.
LEDGER_CAP_BYTES = 5 * 1024 * 1024


def default_ledger_path() -> Path:
    env = os.environ.get("ASTRA_TOKEN_LEDGER")
    if env:
        return Path(env)
    return _DEFAULT_DIR / LEDGER_NAME


# back-compat alias (older name for default_ledger_path)
ledger_path = default_ledger_path


def record_usage(caller: str, model: str, usage: Dict[str, int],
                 ledger_path: Optional[Path] = None,
                 ts: Optional[str] = None) -> None:
    """Append one usage record. Untagged or zero-usage calls are skipped by
    the gateway, so every line here is a real billed call.

    NOTE: ``ledger_path`` the PARAMETER shadows the module-level alias; the
    default-path fallback must call :func:`default_ledger_path` by its own
    name (a 2026-08-20 bug had it call the parameter — crashing every
    gateway call that did not pass an explicit path, i.e. all of them).
    """
    path = Path(ledger_path) if ledger_path is not None else default_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    row = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "caller": caller,
        "model": model,
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def _rotate_if_needed(path: Path) -> None:
    """Rotate the ledger to ``.1`` at the cap (never raises)."""
    try:
        if path.exists() and path.stat().st_size > LEDGER_CAP_BYTES:
            rot = path.with_name(path.name + ".1")
            try:
                rot.unlink()
            except OSError:
                pass
            path.rename(rot)
    except OSError:
        pass


def today_total(ledger_path: Optional[Path] = None) -> Dict[str, int]:
    """Today's (UTC) totals — the number the daily budget circuit breaker reads.

    One pass over the ledger; bounded by the rotation cap."""
    path = Path(ledger_path) if ledger_path is not None else default_ledger_path()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("ts", ""))[:10] != today:
                continue
            out["input_tokens"] += int(row.get("input_tokens", 0))
            out["output_tokens"] += int(row.get("output_tokens", 0))
            out["calls"] += 1
    return out


def daily_totals(ledger_path: Optional[Path] = None) -> Dict[str, Dict[str, int]]:
    """Aggregate the ledger to per-day token totals plus call counts."""
    path = Path(ledger_path) if ledger_path is not None else default_ledger_path()
    totals: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0})
    if not path.exists():
        return {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            day = str(row.get("ts", ""))[:10]
            if not day:
                continue
            totals[day]["input_tokens"] += int(row.get("input_tokens", 0))
            totals[day]["output_tokens"] += int(row.get("output_tokens", 0))
            totals[day]["calls"] += 1
    return dict(totals)
