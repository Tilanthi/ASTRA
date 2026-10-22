"""_llm.py — decoupled loader for the canonical astra_core LLM gateway.

Why this exists
---------------
``evolved_analysis`` runs with ``PYTHONPATH=astra_core/scientific_discovery``
and deliberately does NOT import the ``astra_core`` package (its ``__init__`` is
heavy and has a deadlock/init-hang history — CLAUDE.md v5–v7). But after the
gateway refactor, modules here need ``astra_core.intelligence.llm_gateway``.

This loader gets the SAME canonical gateway module WITHOUT triggering
``astra_core/__init__``: it tries a normal package import first (works when the
repo root is on sys.path, e.g. under the supervisor), and falls back to loading
the gateway file by path (the gateway module is self-contained: stdlib +
``anthropic`` only). Same module, same code path — just no heavy package init.

Used by proposer.py and novelty_gate.py so both work in decoupled ``python -m
evolved_analysis.*`` mode.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_GATEWAY_PATH = REPO_ROOT / "astra_core" / "intelligence" / "llm_gateway.py"
_TOKEN_LEDGER_PATH = REPO_ROOT / "astra_core" / "intelligence" / "token_ledger.py"
_module = None
_ledger_module = None


def gateway_module():
    """Return the loaded ``llm_gateway`` module (cached). Raises on total failure."""
    global _module
    if _module is not None:
        return _module
    # 1) normal package import (repo root on sys.path — e.g. supervisor context)
    try:
        import astra_core.intelligence.llm_gateway as gw  # noqa: F401
        _module = gw
        return _module
    except Exception:
        pass
    # 2) file-path load (decoupled mode; avoids astra_core/__init__)
    if not _GATEWAY_PATH.is_file():
        raise RuntimeError(f"llm_gateway not found at {_GATEWAY_PATH}")
    spec = importlib.util.spec_from_file_location("astra_llm_gateway", _GATEWAY_PATH)
    _module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_module)
    return _module


def get_gateway():
    """Convenience: the cached default gateway instance."""
    return gateway_module().get_gateway()


def token_ledger_module():
    """Return the loaded ``token_ledger`` module (cached), decoupled the same
    way as the gateway. ``token_ledger`` is stdlib-only, so a file-path load is
    always safe. Needed by the raw-client callers (proposer, meta_prompt) that
    bypass ``LLMGateway.complete`` and therefore miss its built-in recording —
    point 7 of the 2026-10-10 token-economy audit."""
    global _ledger_module
    if _ledger_module is not None:
        return _ledger_module
    try:
        import astra_core.intelligence.token_ledger as tl  # noqa: F401
        _ledger_module = tl
        return _ledger_module
    except Exception:
        pass
    if not _TOKEN_LEDGER_PATH.is_file():
        raise RuntimeError(f"token_ledger not found at {_TOKEN_LEDGER_PATH}")
    spec = importlib.util.spec_from_file_location("astra_token_ledger",
                                                  _TOKEN_LEDGER_PATH)
    _ledger_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_ledger_module)
    return _ledger_module


def record_usage(caller: str, model: str, usage: dict,
                 ledger_path=None) -> None:
    """Record one raw-client call's usage to the token ledger (never raises —
    accounting must not break a discovery step)."""
    try:
        kw = {}
        if ledger_path is not None:
            kw["ledger_path"] = ledger_path
        token_ledger_module().record_usage(caller, model, usage, **kw)
    except Exception:
        pass
