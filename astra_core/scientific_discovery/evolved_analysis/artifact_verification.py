"""artifact_verification.py — artifact-level verification at promotion
(v14.19, 2026-09-19).

Paper2Agent-inspired adoption (Miao et al., Nature 2026, arXiv:2509.06917):
their fresh verifier agents test the ARTIFACT — the generated tool, against
injected inputs with known answers — not only the claim. ASTRA's chokepoint
already attacks the CLAIM (fresh-attacker re-splits + permutation null,
second-family novelty audit); this module adds the artifact layer that was
missing, and it travels with every emitted claim:

  ENV MANIFEST   — the emitted record previously carried no environment
     record at all. ``env_manifest`` pins the interpreter, platform, and the
     installed version of every third-party module the program imports (the
     dependency pins a Zenodo deposit needs to be re-runnable).

  SIGNAL CHECK   — ONE sandbox run of the program on the planted-association
     frame (``claim_eval_worker._signal_splits``: every numeric column's
     marginal preserved exactly, a near-deterministic cross-column association
     planted). This is the known-answer bracket's missing half — the attacker's
     permutation null already demands the statistic stay QUIET on no-association
     data; the signal frame demands it FIRE on known-association data. A
     statistic that misses a planted ~0.94-rank-correlation relation cannot be
     trusted to have measured the subtler real one.

  HARNESS LABEL  — rule-based, never composed freehand (same discipline as
     evidence_ledger.confidence_label):
       harness-passed        signal detected (p <= the same family-corrected
                             bar the verdict itself used)
       harness-marked        signal NOT detected on a definitive run — the
                             artifact is flagged on the review surface and a
                             ledger note records it (visible, non-blocking)
       harness-not-runnable  the harness run errored / timed out — recorded as
                             itself, never a pass
       harness-inconclusive  the program returned no p-value on the signal
                             frame (cannot be judged; not flagged)

Honesty guarantees: NEVER blocks emission (a maybe-wrong generic harness must
not kill a possibly-good claim — the mark is the visible, reviewable outcome,
exactly like the attacker's `suspect`); never raises; never fabricates a pass;
writes the ledger note ONLY on a definitive not-detected (an inconclusive or
erroring harness is its own record, not an accusation).

Public API: ``verify_artifact(verdict) -> dict`` (never raises).
Run:
    PYTHONPATH=astra_core/scientific_discovery python -m evolved_analysis.artifact_verification
"""
from __future__ import annotations

import logging
import re
import sys

logger = logging.getLogger(__name__)

# One signal run per promotion (the attacker costs 5; this adds a 6th, only
# for near-promotions — after the attacker survived and the second judge did
# not block).
SIGNAL_SEED = 31416
SIGNAL_TIMEOUT = 90.0


def env_manifest(source: str) -> dict:
    """Pin the execution environment for an artifact: interpreter, platform,
    and the installed version of every third-party top-level module the
    program imports. Stdlib names carry no distribution metadata and are
    naturally absent. Deterministic (sorted keys)."""
    import platform
    mods = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)",
                         source or "", re.MULTILINE):
        mods.add(m.group(1))
    pins = {}
    for mod in sorted(mods):
        try:
            from importlib import metadata
            version = metadata.version(mod)
        except Exception:            # stdlib / not-a-distribution: absent
            continue
        pins[mod] = version
    return {"python": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "platform": platform.platform(),
            "package_pins": pins}


def signal_check(src: str, source: str = "legacy", pmax: float = None) -> dict:
    """ONE sandbox run of the program on the planted-association frame.

    Returns the known-answer report (never raises): ``status`` is
    ``detected`` | ``not-detected`` | ``no-pvalue`` | ``error``.
    """
    if pmax is None:
        from .claim_gates import bonferroni_pmax
        from .claim_task import PMAX
        pmax = bonferroni_pmax(PMAX)
    try:
        from .run_claim_search import gate1_run   # lazy: stub-friendly
        m = gate1_run(src, seed=SIGNAL_SEED, source=source,
                      timeout=SIGNAL_TIMEOUT, signal_seed=SIGNAL_SEED)
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}
    if m.get("error"):
        return {"status": "error", "error": str(m["error"])[:200]}
    p = m.get("pvalue")
    if p is None:
        return {"status": "no-pvalue", "effect": m.get("effect")}
    p = float(p)
    detected = p <= pmax
    return {"status": "detected" if detected else "not-detected",
            "effect": m.get("effect"), "pvalue": p, "pmax": pmax}


_LABEL_RULES = {
    "detected": "harness-passed",
    "not-detected": "harness-marked",
    "error": "harness-not-runnable",
    "no-pvalue": "harness-inconclusive",
}


def harness_label(signal_status: str) -> str:
    """Rule-based label from the signal-check status (never freehand)."""
    return _LABEL_RULES.get(signal_status, "harness-inconclusive")


def verify_artifact(verdict: dict) -> dict:
    """Assemble the artifact-verification report for a near-promotion verdict.

    Non-blocking and defensive by design: the report is attached to the
    verdict and travels with the emitted record; only a DEFINITIVE
    not-detected writes a ledger note (visible flag). Never raises, never
    blocks, never fabricates a pass.
    """
    base = {"harness_label": "harness-not-runnable"}
    try:
        src = verdict.get("source") or ""
        dataset = verdict.get("dataset") or "legacy"
        env = env_manifest(src)
        pmax = (verdict.get("gate1") or {}).get("bonferroni_pmax")
        sig = signal_check(src, source=dataset, pmax=pmax)
        label = harness_label(sig.get("status"))
        report = {**base, "env": env, "signal_check": sig,
                  "harness_label": label}
        if sig.get("status") == "not-detected":
            # definitive unresponsiveness on the known-answer frame: visible
            # flag, never a silent trust, never a block
            try:
                from .evidence_ledger import safe_append
                safe_append("note", "artifact-harness-flagged",
                            dataset=verdict.get("dataset"),
                            claim=(verdict.get("claim") or "")[:200],
                            program_hash=verdict.get("program_hash"),
                            value=sig.get("effect"),
                            detail="statistic did not fire on the planted-"
                                   "association frame "
                                   f"(p={sig.get('pvalue')} > pmax={sig.get('pmax')})")
            except Exception:
                pass
        return report
    except Exception as e:
        logger.warning("[artifact_verification] verify failed: %s", e)
        return {**base, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def main() -> int:
    """Zero-spend smoke: label rules + env manifest of this module's imports."""
    import json
    print(json.dumps({
        "label_rules": _LABEL_RULES,
        "self_env": env_manifest("import json\nimport numpy\nimport pandas\n"),
        "signal_seed": SIGNAL_SEED,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
