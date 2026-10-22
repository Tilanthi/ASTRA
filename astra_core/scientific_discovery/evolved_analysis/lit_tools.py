"""lit_tools.py — papers-as-tested-tools registry (v14.19, 2026-09-19).

Paper2Agent-inspired adoption (Miao, Davis, Zhang, Pritchard & Zou, Nature 2026,
arXiv:2509.06917): instead of an LLM's RECOLLECTION of a literature method, the
method's own code becomes a callable, TESTED tool. Two use sites inside ASTRA:

  * the model-falsification arm — a ``FalsifiablePrediction`` may set
    ``lit_tool`` to a registered tool_id whose ``run.py`` implements its
    ``predict()``; ``engine.evaluate`` then stamps the result
    ``executed_via="lit-tool:<id>"`` (the literature's own code, not a
    recollection), and files an on-demand request when a named tool is absent;
  * any gate/verifier that wants execution-level evidence — ``run()`` executes
    a VERIFIED tool under the JSON job contract with a timeout.

Honesty contract (the fiction rules apply in full)
--------------------------------------------------
- The registry starts EMPTY and only ever grows through ``register()``, which
  requires a real tool directory on disk whose self-test PASSES in a real
  subprocess. This module never invents, downloads, or pre-populates tools —
  an agentified paper is built by an agent session (clone the cited repo,
  write the wrapper, write the self-test) and only then registered.
- Rejected registration attempts are RECORDED (append-only history), never
  silently dropped; entries whose directory disappears are marked ``broken``,
  never deleted.
- "Agentification on demand, not blanket" (the adoption decision): the build
  step is spend-gated and NOT automated here. ``request()`` only queues the
  need with its scientific justification, deduped, in the registry + ledger.

Tool contract (a directory anywhere on disk, recorded by absolute path):
    TOOL.json      {"tool_id", "paper_title", "paper_year", "code_url",
                    "arxiv_or_doi", "method", "notes" (optional)}
    run.py         invoked ``python run.py <job.json>``; prints ONE JSON line
    selftest.py    invoked ``python selftest.py``; exit 0 + one JSON line with
                   ``"ok": true`` = the harness passes (known-answer test of
                   the wrapper against the paper's own published behaviour)

Run:
    PYTHONPATH=astra_core/scientific_discovery python -m evolved_analysis.lit_tools status
    ... register <tool_dir> | verify <tool_id> | run <tool_id> <job.json> | request
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PERSIST = Path.home() / ".astra_persistent"
EVOLVED_PROGRAMS = PERSIST / "evolved_programs"
REGISTRY = EVOLVED_PROGRAMS / "lit_tools.json"

# Bounded compute per tool interaction (same discipline as the fresh-attacker).
SELFTEST_TIMEOUT = 120.0
RUN_TIMEOUT = 120.0

# Minimal, deterministic environment pins stamped on every verification event.
_REQUIRED_META = ("tool_id", "paper_title", "paper_year", "code_url",
                  "arxiv_or_doi", "method")
_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _env_pins() -> dict:
    import platform
    return {"python": sys.version.split()[0], "platform": platform.platform()}


def _slug(text: str) -> str:
    return _ID_RE.sub("-", (text or "").strip().lower()).strip("-")[:60]


def _load() -> dict:
    """Read the registry ({} on missing/corrupt). Entries whose tool directory
    has disappeared are marked ``broken`` (recorded as an event, never deleted)."""
    reg = {"tools": [], "requests": []}
    try:
        if REGISTRY.exists():
            disk = json.loads(REGISTRY.read_text() or "{}")
            reg["tools"] = disk.get("tools") or []
            reg["requests"] = disk.get("requests") or []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[lit_tools] registry unreadable, starting clean: %s", e)
    for t in reg["tools"]:
        if t.get("status") == "verified" and not Path(t.get("tool_dir", "")
                                                      ).is_dir():
            t["status"] = "broken"
            t.setdefault("history", []).append(
                {"ts": _now(), "event": "selftest", "ok": False,
                 "detail": "tool directory disappeared"})
    return reg


def _save(reg: dict) -> None:
    EVOLVED_PROGRAMS.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2) + "\n")


def _run_selftest(tool_dir: Path, timeout: float = SELFTEST_TIMEOUT) -> dict:
    """Run one tool's self-test in a real subprocess. Never raises."""
    try:
        proc = subprocess.run(
            [sys.executable, "selftest.py"], cwd=str(tool_dir),
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"selftest timeout > {timeout:g}s"}
    except Exception as e:
        return {"ok": False,
                "detail": f"spawn:{type(e).__name__}:{str(e)[:120]}"}
    out = proc.stdout.strip().splitlines()
    parsed = None
    if out:
        try:
            parsed = json.loads(out[-1])
        except json.JSONDecodeError:
            parsed = None
    ok = proc.returncode == 0 and isinstance(parsed, dict) and parsed.get("ok") is True
    detail = ("ok" if ok else
              (parsed or {}).get("detail") if isinstance(parsed, dict) else None)
    return {"ok": ok, "returncode": proc.returncode,
            "detail": (detail or (out[-1][:120] if out else "no stdout")
                       if not ok else "ok"),
            "env": _env_pins()}


def register(tool_dir: str, timeout: float = SELFTEST_TIMEOUT) -> dict:
    """Validate one tool directory against the contract and register it.

    Registration is HONEST: the metadata must be complete, the entry points
    must exist, and the self-test must pass in a real subprocess — otherwise
    the entry is recorded with status ``rejected`` (kept, with reasons) and is
    never runnable. Re-registering a verified tool re-runs its self-test
    (a fresh verification event, not a second entry).
    """
    d = Path(tool_dir).expanduser().resolve()
    rec = {"tool_id": None, "tool_dir": str(d), "registered_ts": _now(),
           "paper": {}, "status": "rejected", "history": []}
    problems = []
    if not d.is_dir():
        problems.append(f"not a directory: {d}")
    else:
        meta_path = d / "TOOL.json"
        if not meta_path.is_file():
            problems.append("missing TOOL.json")
        else:
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError as e:
                meta, problems = {}, [f"TOOL.json unparseable: {e}"]
            missing = [k for k in _REQUIRED_META if not str(meta.get(k, "")).strip()]
            if missing:
                problems.append(f"TOOL.json missing/empty: {missing}")
            for ep in ("run.py", "selftest.py"):
                if not (d / ep).is_file():
                    problems.append(f"missing {ep}")
            if not missing:
                rec["tool_id"] = _slug(str(meta["tool_id"]))
                if not rec["tool_id"]:
                    problems.append("tool_id slugs to empty")
                rec["paper"] = {k: meta.get(k) for k in
                                ("paper_title", "paper_year", "code_url",
                                 "arxiv_or_doi", "method", "notes")
                                if meta.get(k) is not None}
    if problems:
        rec["history"].append({"ts": _now(), "event": "register", "ok": False,
                               "detail": "; ".join(problems)[:300]})
        _record_registration(rec)
        return rec

    reg = _load()
    # a prior entry for this tool_id, or an earlier REJECTED attempt on the
    # same directory, is REPLACED (history kept) — never duplicated
    prior = next((t for t in reg["tools"]
                  if t.get("tool_id") == rec["tool_id"]
                  or t.get("tool_dir") == rec["tool_dir"]), None)
    test = _run_selftest(d, timeout=timeout)
    rec["history"].append({"ts": _now(), "event": "register",
                           "ok": test["ok"], "detail": test["detail"],
                           "env": test.get("env")})
    rec["status"] = "verified" if test["ok"] else "rejected"
    rec["last_env"] = test.get("env") or {}
    if prior is None:
        reg["tools"].append(rec)
        _save(reg)
    else:  # same tool_id: the new attempt replaces the record, history kept
        prior.update({k: v for k, v in rec.items() if k != "history"})
        prior["history"] = (prior.get("history", [])
                            + [h for h in rec["history"]])
        _save(reg)
    return rec


def _record_registration(rec: dict) -> None:
    """Keep a rejected attempt visible (append-only honesty)."""
    reg = _load()
    prior = next((t for t in reg["tools"]
                  if t.get("tool_dir") == rec["tool_dir"]
                  and not t.get("tool_id")), None)
    if prior is None:
        reg["tools"].append(rec)
    else:
        prior["history"] += rec["history"]
    _save(reg)


def get(tool_id: str) -> dict | None:
    return next((t for t in _load()["tools"]
                 if t.get("tool_id") == (tool_id or "").strip()), None)


def verify(tool_id: str, timeout: float = SELFTEST_TIMEOUT) -> dict:
    """Re-run one registered tool's self-test; stamp the outcome honestly
    (``verified`` / ``failed`` / ``broken``). Never raises."""
    tool = get(tool_id)
    if tool is None:
        return {"tool_id": tool_id, "status": "unknown",
                "detail": "no such tool registered"}
    d = Path(tool.get("tool_dir", ""))
    if not d.is_dir():
        tool["status"] = "broken"
        tool.setdefault("history", []).append(
            {"ts": _now(), "event": "selftest", "ok": False,
             "detail": "tool directory disappeared"})
        _save(_load_with(tool))
        return {"tool_id": tool_id, "status": "broken",
                "detail": "tool directory disappeared"}
    test = _run_selftest(d, timeout=timeout)
    tool["status"] = "verified" if test["ok"] else "failed"
    tool["last_env"] = test.get("env") or {}
    tool.setdefault("history", []).append(
        {"ts": _now(), "event": "selftest", "ok": test["ok"],
         "detail": test["detail"], "env": test.get("env")})
    _save(_load_with(tool))
    return {"tool_id": tool_id, "status": tool["status"],
            "detail": test["detail"]}


def _load_with(tool: dict) -> dict:
    """Registry with ``tool`` merged over its existing entry (by tool_id/dir)."""
    reg = _load()
    for i, t in enumerate(reg["tools"]):
        if (t.get("tool_id") == tool.get("tool_id")
                and t.get("tool_dir") == tool.get("tool_dir")) \
                or (not t.get("tool_id") and t.get("tool_dir") == tool.get("tool_dir")):
            reg["tools"][i] = tool
            return reg
    reg["tools"].append(tool)
    return reg


def run(tool_id: str, job: dict, timeout: float = RUN_TIMEOUT) -> dict:
    """Execute a VERIFIED tool under the JSON job contract.

    Only ``verified`` tools run — a failed/broken/rejected entry is refused
    (running unverified literature code would be exactly the fiction-adjacent
    shortcut this registry exists to prevent). Returns a report dict, never
    raises: ``{"ok", "result"|"error"}``.
    """
    tool = get(tool_id)
    if tool is None:
        return {"ok": False, "error": f"unknown tool {tool_id!r}"}
    if tool.get("status") != "verified":
        return {"ok": False,
                "error": f"tool {tool_id!r} status is {tool.get('status')!r}, "
                         f"not 'verified' — refusing to run"}
    d = Path(tool["tool_dir"])
    job_file = d / ".astra_job.json"
    try:
        job_file.write_text(json.dumps(job))
        proc = subprocess.run(
            [sys.executable, "run.py", str(job_file)], cwd=str(d),
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"run timeout > {timeout:g}s"}
    except Exception as e:
        return {"ok": False, "error": f"spawn:{type(e).__name__}:{str(e)[:160]}"}
    finally:
        try:
            job_file.unlink()
        except OSError:
            pass
    out = proc.stdout.strip().splitlines()
    if not out:
        return {"ok": False, "error": (proc.stderr.strip()[:200] or "no stdout")}
    try:
        return {"ok": True, "result": json.loads(out[-1])}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"unparseable output: {out[-1][:160]}"}


def request(paper_title: str, code_url: str = None, why: str = "",
            *, tool_id: str = None, claim: str = None, dataset=None) -> dict:
    """Queue an on-demand agentification request (deduped), and ledger it.

    This is the "agentify on demand, never blanket" discipline: the request
    records WHAT literature method a live discovery path needs and WHY — the
    actual build (clone the cited repo, write + self-test the wrapper) is a
    spend-gated agent session, never fabricated here.
    """
    reg = _load()
    key = tool_id or f"{(paper_title or '').strip().lower()}|{(code_url or '').strip().lower()}"
    for r in reg["requests"]:
        if r.get("key") == key:
            return {**r, "duplicate": True}   # already queued — no second entry
    req = {"ts": _now(), "key": key, "tool_id": tool_id,
           "paper_title": (paper_title or "")[:200],
           "code_url": code_url, "why": (why or "")[:300],
           "claim": (claim or "")[:200] if claim else None,
           "dataset": dataset, "status": "pending"}
    reg["requests"].append(req)
    _save(reg)
    try:
        from .evidence_ledger import safe_append
        safe_append("note", "lit-tool-requested", dataset=dataset, claim=claim,
                    detail=f"{req['paper_title']} — {req['why']}"[:300])
    except Exception:
        pass
    return req


def missing_tool_note(tool_id: str, context: str = "") -> str:
    """File (once per pending state) the on-demand request for a tool a
    discovery path NAMED but the registry does not hold. Returns a short
    provenance note for the caller to stamp on its result."""
    req = request(paper_title=f"(named-but-unregistered tool: {tool_id})",
                  why=context or "named by a discovery path; not registered",
                  tool_id=tool_id)
    return ("lit-tool-requested" if not req.get("duplicate")
            else "lit-tool-request-already-pending")


def provenance(tool_id: str) -> dict | None:
    """Provenance stamp for a VERIFIED tool (else None — never fabricated)."""
    tool = get(tool_id)
    if tool and tool.get("status") == "verified":
        last = [h for h in tool.get("history", []) if h.get("event") == "selftest"]
        return {"executed_via": f"lit-tool:{tool_id}",
                "paper": tool.get("paper"),
                "verified_at": (last[-1]["ts"] if last
                                else tool.get("registered_ts"))}
    return None


def status() -> dict:
    reg = _load()
    tools = reg["tools"]
    return {
        "n_tools": len(tools),
        "by_status": {s: sum(1 for t in tools if t.get("status") == s)
                      for s in ("verified", "rejected", "failed", "broken")
                      if any(t.get("status") == s for t in tools)},
        "n_pending_requests": sum(1 for r in reg["requests"]
                                  if r.get("status") == "pending"),
        "tools": [{"tool_id": t.get("tool_id"), "status": t.get("status"),
                   "paper": (t.get("paper") or {}).get("paper_title")}
                  for t in tools],
        "pending_requests": [{"tool_id": r.get("tool_id") or r.get("key"),
                              "paper_title": r.get("paper_title"),
                              "why": r.get("why")} for r in reg["requests"]
                             if r.get("status") == "pending"][:10],
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["status", "register", "verify", "run",
                                    "request"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif a.cmd == "register":
        if not a.args:
            print("usage: register <tool_dir>")
            return 1
        print(json.dumps(register(a.args[0]), indent=2))
        return 0
    elif a.cmd == "verify":
        if not a.args:
            print("usage: verify <tool_id>")
            return 1
        print(json.dumps(verify(a.args[0]), indent=2))
    elif a.cmd == "run":
        if len(a.args) < 2:
            print("usage: run <tool_id> <job.json>")
            return 1
        job = json.loads(Path(a.args[1]).read_text())
        print(json.dumps(run(a.args[0], job), indent=2)[:2000])
    elif a.cmd == "request":
        if len(a.args) < 2:
            print("usage: request <paper_title> <code_url> [why]")
            return 1
        why = a.args[2] if len(a.args) > 2 else ""
        print(json.dumps(request(a.args[0], a.args[1], why), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
