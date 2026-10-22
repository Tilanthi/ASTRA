"""_io_tail.py — bounded backward reads over append-only JSONL stores.

The token-economy audit (2026-10-10) found the discovery loop re-parsing whole
stores per call: a 4.5 MB verdict log per episode (three different readers), a
43.6 MB novelty cache per candidate, and a growing evidence ledger on every
append. This module reads those files from the END, which is where the rows any
recent-window query needs live (the logs are append-only and already rotate at
20 MB — rows older than the rotation boundary were never readable anyway).

Stdlib-only on purpose (same rule as ``claim_gates`` / ``evidence_ledger``) so
the sandboxed worker and the decoupled evolved_analysis package can import it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def tail_jsonl(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> List[dict]:
    """Parse the LAST ``max_bytes`` of a JSONL file into rows (FILE ORDER,
    oldest-first within what was read). Malformed lines are skipped."""
    rows: List[dict] = []
    window = _read_tail(path, max_bytes)
    if window is None:
        return rows
    data, started_mid_line = window
    if started_mid_line:
        # the window opened inside a record: discard its cut fragment at the
        # BYTE level. (Dropping the first PARSED row instead — the earlier
        # version — was wrong twice over: the fragment usually fails to parse
        # and gets skipped, so the drop removed a COMPLETE row; with a tight
        # window that emptied the result entirely.)
        nl = data.find("\n")
        data = "" if nl < 0 else data[nl + 1:]
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def last_matching(path: Path, predicate: Callable[[dict], bool], n: int,
                  max_bytes: int = DEFAULT_MAX_BYTES
                  ) -> Tuple[List[dict], bool]:
    """The last ``n`` rows matching ``predicate``, in file order.

    Reads backward in chunks and stops as soon as ``n`` matches are collected,
    so a query for a dataset's recent window costs the tail, not the whole
    file. Returns ``(rows, hit_cap)``; ``hit_cap`` is True when the byte cap
    was reached before ``n`` matches were found (the caller decides whether the
    partial window is acceptable — for the liveness windows in this package it
    is, and callers that need exactness can re-read in full)."""
    if not path.exists():
        return [], False
    matches: List[dict] = []
    offset_end = path.stat().st_size
    chunk = 256 * 1024
    floor = max(0, offset_end - max_bytes)
    hit_cap = False
    while offset_end > 0:
        offset_start = max(0, offset_end - chunk)
        if offset_start < floor:
            offset_start = floor
            hit_cap = True
        with path.open("rb") as f:
            f.seek(offset_start)
            blob = f.read(offset_end - offset_start).decode(
                "utf-8", errors="replace")
        lines = blob.splitlines()
        # the first line is cut mid-record unless we started at 0 — drop it
        if offset_start > 0 and lines:
            lines = lines[1:]
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if predicate(rec):
                matches.append(rec)
                if len(matches) >= n:
                    matches.reverse()
                    return matches, hit_cap
        if offset_start == 0:
            break
        if hit_cap and offset_start <= floor:
            break
        offset_end = offset_start
    matches.reverse()
    return matches, hit_cap


def _read_tail(path: Path, max_bytes: int) -> Optional[Tuple[str, bool]]:
    """Read the last <=max_bytes of ``path``. Returns (text, started_mid_line)
    — started_mid_line is True when the window began INSIDE a record (the byte
    before the window start is not a newline), so the fragment up to the first
    newline is cut and must be discarded, not parsed."""
    if not path.exists():
        return None
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            pos = f.seek(size - max_bytes)
            # look one byte back to know whether the window opened mid-line
            f.seek(pos - 1)
            started_mid_line = f.read(1) != b"\n"
            f.seek(pos)
        else:
            started_mid_line = False
        return f.read().decode("utf-8", errors="replace"), started_mid_line
