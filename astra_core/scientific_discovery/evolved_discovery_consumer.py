"""evolved_discovery_consumer.py — bridge evolved_analysis → ASTRA's discovery loop.

The auto-start discovery loop calls consume_evolved_discoveries(system) once per
cycle. It folds any NEW machine-verified records from
~/.astra_persistent/evolved_discoveries.json into the loop's in-memory store
(``system.genuine_discoveries``), shaped as ASTRA discoveries with
``validation.quality='GENUINE'`` / ``is_genuine=True`` (they ARE genuine — verified
by code execution on real data, unlike ASTRA's LLM-self-judged discoveries). The
loop's existing _save_discovery_store then persists them in the GENUINE bucket.

Design constraints (the loop has a deadlock/blocking history, CLAUDE.md v5–v7):
  - NEVER raises — every failure is logged and swallowed.
  - No locks, no blocking I/O, no network. One small file read + list append.
  - Idempotent across cycles AND service restarts: dedup by
    verification.program_hash against what is already in system.genuine_discoveries.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVOLVED_STORE = lambda: Path.home() / ".astra_persistent" / "evolved_discoveries.json"

# Token-economy point 12 (2026-10-10): the store is re-read and re-parsed every
# supervisor cycle even when nothing changed. A cheap (mtime_ns, size) signature
# skips the parse for an unchanged store; _SEEN_HASHES keeps the fold idempotent
# when the store GROWS between cycles (only the appended records are new).
_LAST_SEEN = None
_SEEN_HASHES = set()


def _store_signature(path: Path):
    """Cheap change signature for the store: (mtime_ns, size), or None."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _store_unchanged(path: Path) -> bool:
    """True when the store's signature matches the last successful parse."""
    sig = _store_signature(path)
    return sig is not None and sig == _LAST_SEEN


def consume_evolved_discoveries(system: Any) -> int:
    """Fold new evolved discoveries into system.genuine_discoveries.

    Returns the number newly ingested (0 if none/new). Never raises."""
    global _LAST_SEEN
    try:
        path = EVOLVED_STORE()
        if not path.exists():
            return 0
        sig = _store_signature(path)
        if sig is not None and sig == _LAST_SEEN:
            return 0  # unchanged store: no read, no parse
        raw = path.read_text()
        records = json.loads(raw)
        _LAST_SEEN = sig
        if not isinstance(records, list) or not records:
            return 0

        # dedup against what the system already holds (survives restarts)
        existing = set()
        for d in getattr(system, "genuine_discoveries", []) or []:
            if not isinstance(d, dict) or d.get("source") != "evolved_analysis":
                continue
            v = d.get("verification") or {}
            h = v.get("program_hash") or d.get("timestamp")
            if h:
                existing.add(h)

        n = 0
        for r in records:
            if not isinstance(r, dict):
                continue
            v = r.get("verification") or {}
            h = v.get("program_hash") or r.get("timestamp")
            if not h or h in existing or h in _SEEN_HASHES:
                continue
            existing.add(h)
            _SEEN_HASHES.add(h)
            discovery = {
                "title": r.get("title", "Evolved analysis pipeline"),
                "abstract": r.get("abstract", ""),
                "discovery_type": r.get("discovery_type", "machine_verified_analysis"),
                "timestamp": r.get("timestamp"),
                # ASTRA-shaped validation: machine-verified => genuinely GENUINE
                "validation": {
                    "quality": "GENUINE",
                    "is_genuine": True,
                    "confidence": 1.0,
                    "note": "machine-verified by code execution on real archival "
                            "data (NOT LLM self-judgment)",
                },
                "verification": v,            # the proof (metric, held-out value, n, …)
                "source": "evolved_analysis",
                # Fix 1 (e2e): carry the persisted run_claim source so a claim in
                # the genuine store can be independently re-run / audited.
                "program_source": r.get("program_source"),
            }
            system.genuine_discoveries.append(discovery)
            n += 1
            # Phase-2 claims carry effect/hold-out under verification; Phase-1
            # pipelines carry eval_value/held_out_test_value. Report whichever.
            logger.info("[GenuineDiscovery] ✅ INGESTED machine-verified evolved "
                        "discovery: %s [%s=%s, holdout=%s]", discovery["title"][:60],
                        v.get("metric_name", "?"),
                        v.get("effect", v.get("eval_value", "?")),
                        v.get("held_out_test_value",
                              v.get("effect_insample", "?")))
        if n:
            logger.info("[GenuineDiscovery] consumed %d new evolved discovery(ies) "
                        "into the genuine store.", n)
        return n
    except Exception as e:  # never let the consumer crash/block the discovery loop
        logger.warning("[GenuineDiscovery] consume_evolved_discoveries skipped: %s", e)
        return 0
