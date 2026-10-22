"""proposer.py — AlphaEvolve-style "creative generation".

Given a parent program (source + metrics) and inspirations (other good
programs), a Proposer produces a CHILD program source. Two backends:

  * LLMProposer     — asks Claude (via the configured gateway) for an improvement,
                      as either a search/replace DIFF or a full rewrite. This is
                      the real AlphaEvolve path.
  * GeneticProposer — offline fallback: mutates a structured spec and re-renders
                      source. Always available; fully reproducible.

apply_diff() parses the <<<SEARCH ... >>> <<<REPLACE ... >>> block format and
falls back to a fenced full-rewrite, then to the parent unchanged.
"""
from __future__ import annotations

import os
import re
import copy
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .program import (
    render_source, validate_source, NAIVE_SPEC)

ALL_PAIRS = ["ug", "gr", "ri", "iz"]

# Parse-failure capture (token-economy point 6, 2026-10-10): an unparseable
# proposer response used to vanish (mode="unchanged", nothing kept), so the
# root cause of every wasted call was unauditable. Truncated raw responses are
# dumped here instead — small, bounded, honest.
PROPOSER_FAILURES_DIR = (Path.home() / ".astra_persistent" / "evolved_programs"
                         / "proposer_failures")
_FAILURE_DUMP_MAX = 4096


# --------------------------------------------------------------------------- #
# diff parsing / application                                                   #
# --------------------------------------------------------------------------- #

_SEARCH_RE = re.compile(
    r"<<<\s*SEARCH\s*>>>(.*?)<<<\s*REPLACE\s*>>>(.*?)<<<\s*END\s*>>>",
    re.DOTALL)


def extract_python_block(text: str) -> Optional[str]:
    m = re.search(r"```python\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # bare ``` fence (no language tag) — models emit these often enough that
    # rejecting them wasted whole proposer calls (2026-10-10 audit)
    m = re.search(r"```\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _cap_source(src: str, budget: int = 7500) -> str:
    """Cap a source listing for the PROMPT (head + elision marker + tail).

    The full parent is still what diffs apply against; this only bounds what
    goes into the message. Keeps the signature (usually in the head) and the
    return/result tail (the part the model most needs to see)."""
    if len(src) <= budget:
        return src
    head = min(2000, budget // 3)
    tail = max(0, budget - head - 80)
    marker = f"\n# ... [{len(src) - head - tail} chars elided]\n"
    return src[:head] + marker + src[-tail:]


def apply_diff(parent_src: str, response: str, entry_point: str = "estimate_redshift") -> Tuple[str, str]:
    """Return (child_source, mode) where mode in
    {'diff','diff-partial','rewrite','unchanged'}. Never raises.

    Tolerant by design (2026-10-10): a SEARCH block that fails to match no
    longer discards the whole response — the remaining blocks still apply
    ('diff-partial'), and when nothing applied (or the result is invalid) the
    fenced full-rewrite fallback runs instead of bailing to 'unchanged'. The
    old bail-on-first-failure threw away valid rewrites that followed a single
    bad block, burning the LLM call for nothing."""
    # 1) try SEARCH/REPLACE blocks
    blocks = _SEARCH_RE.findall(response)
    if blocks:
        out = parent_src
        applied = skipped = 0
        for old, new in blocks:
            old_s, new_s = old.strip("\n"), new.strip("\n")
            if old_s and old_s in out:
                out = out.replace(old_s, new_s, 1)
                applied += 1
            else:
                skipped += 1   # tolerate: later blocks may still apply
        if applied and validate_source(out, entry_point):
            return out, ("diff" if skipped == 0 else "diff-partial")
    # 2) try a fenced full rewrite (also the fallback when no block applied
    #    or the diff result failed validation)
    py = extract_python_block(response)
    if py and validate_source(py, entry_point):
        return py, "rewrite"
    # 3) last resort: maybe the whole response IS the function
    if validate_source(response, entry_point):
        return response.strip(), "rewrite"
    return parent_src, "unchanged"


# --------------------------------------------------------------------------- #
# proposer interface + backends                                               #
# --------------------------------------------------------------------------- #

class Proposer:
    name = "base"

    def propose(self, parent_source: str, parent_metrics: dict,
                parent_spec: Optional[dict], inspirations: List[str]
                ) -> Tuple[Optional[str], Optional[dict], dict]:
        """Return (child_source, child_spec, info).
        child_source None means 'could not propose'."""
        raise NotImplementedError


class LLMProposer(Proposer):
    name = "llm"

    def __init__(self, model: Optional[str] = None,
                 max_tokens: int = 1600, timeout: float = 90.0,
                 context_level: str = "rich",
                 task_system: Optional[str] = None,
                 entry_point: str = "estimate_redshift"):
        # Load the canonical gateway decoupled from astra_core/__init__ (evolved_analysis
        # runs with PYTHONPATH=astra_core/scientific_discovery). See _llm.py.
        from ._llm import gateway_module
        LLMGateway = gateway_module().LLMGateway
        self._gw = LLMGateway(
            model=(model or os.environ.get("ASTRA_PROPOSER_MODEL",
                                           "claude-sonnet-5-20250929")),
            max_tokens=max_tokens, timeout=timeout)
        self.client = self._gw.client
        self.model = self._gw.model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.context_level = context_level   # "minimal" | "rich"
        # Task generality (WP5): an alternative task system prompt + entry point.
        # Defaults preserve the photo-z behaviour exactly.
        self.task_system = task_system
        self.entry_point = entry_point
        self.n_calls = 0
        self.n_errors = 0
        self.n_parse_failures = 0
        self._failure_dir = PROPOSER_FAILURES_DIR

    def _dump_parse_failure(self, raw: str) -> None:
        """Keep a truncated copy of an unparseable response (never raises)."""
        try:
            self._failure_dir.mkdir(parents=True, exist_ok=True)
            name = (f"fail_{time.strftime('%Y%m%dT%H%M%S')}_"
                    f"{self.n_parse_failures}.txt")
            (self._failure_dir / name).write_text((raw or "")[:_FAILURE_DUMP_MAX])
        except Exception:
            pass

    BASE_SYSTEM = (
        "You are an expert astronomer and ML engineer. You improve a Python "
        "function `estimate_redshift(df_train, df_eval)` that predicts "
        "spectroscopic redshift (z_spec) of galaxies from SDSS u,g,r,i,z model "
        "magnitudes. It is graded on REAL held-out galaxies by two metrics: "
        "sigma_NMAD (robust scatter) and eta (outlier fraction, |dz|/(1+z)>0.15). "
        "Lower is better.\n"
        "HARD RULES:\n"
        "- Keep the EXACT signature: def estimate_redshift(df_train, df_eval)\n"
        "- df_train has columns u,g,r,i,z AND z_spec. df_eval has u,g,r,i,z.\n"
        "- Return a 1-D numpy array of predictions for df_eval rows (same order).\n"
        "- You may import ONLY: numpy, scipy, sklearn, pandas (already available).\n"
        "- No file I/O, no network, no plotting, no global side effects.\n"
        "- Be correct and self-contained.\n"
        "RESPOND WITH EITHER:\n"
        "  (a) one or more diff blocks (use this exact format):\n"
        "<<<SEARCH>>>\nexact existing code to replace\n<<<REPLACE>>>\nnew code\n<<<END>>>\n"
        "  (b) a single complete rewritten function in one ```python``` block.\n"
        "Prefer small targeted diffs. Output ONLY the diff or code, no explanation."
    )

    # keep SYSTEM alias so any external refs still work
    SYSTEM = BASE_SYSTEM

    # Engineering rules (2026-10-20, adapted from the Ponytail/Karpathy ruleset
    # review): appended MECHANICALLY in propose() to every system prompt —
    # including task_system variants — because rules left to the model's own
    # discretion to invoke activate ~never (JetBrains finding: a plain skill
    # file self-activated 0/10 sessions). Sandbox-adapted: the "codebase" for
    # an evolved program is its parent + inspirations + numpy/scipy/sklearn.
    ENGINEERING_RULES = (
        "\n\nENGINEERING RULES (every proposal):\n"
        "- Reuse before you build: if the parent program or an inspiration "
        "already contains a helper or step that does what you need, reuse or "
        "generalise it instead of duplicating it; prefer an established "
        "numpy/scipy/sklearn routine over a hand-rolled equivalent.\n"
        "- Fix the root, not the symptom: if a helper is wrong, change the "
        "helper once, and make your SEARCH blocks cover EVERY place the defect "
        "lives — a minimal patch at one call site of a shared bug is a second "
        "bug, not a smaller one.\n"
        "- Never simplify away: NaN/invalid-input guards, clipping or checks "
        "that prevent garbage predictions, and anything keeping the output "
        "well-formed. Shorter code must never mean less safe predictions."
    )

    def _render_diagnostics(self, m: dict) -> str:
        if not m:
            return "(no diagnostics available)"
        if "balanced_accuracy" in m:           # classification task (WP5)
            r = {k: m[k] for k in ("recall_STAR", "recall_GALAXY", "recall_QSO")
                 if k in m}
            parts = " ".join(f"{k.split('_')[1]}:{v:.2f}" for k, v in r.items())
            return (f"balanced_accuracy={m['balanced_accuracy']:.3f}  "
                    f"accuracy={m.get('accuracy',0):.3f}  per-class recall [{parts}]")
        if "sigma_nmad" not in m:
            return "(no diagnostics available)"
        lines = [f"sigma_NMAD={m['sigma_nmad']:.4f}  eta={m['eta']:.3f}  "
                 f"bias={m.get('bias', 0):+.4f}"]
        prof = m.get("profile") or []
        if prof:
            parts = [f"z~{b['z']}: med_dz={b['med_dz']:+.4f} (n={b['n']})"
                     for b in prof]
            lines.append("Residual profile by redshift bin: " + " | ".join(parts))
        return "\n".join(lines)

    def propose(self, parent_source, parent_metrics, parent_spec, inspirations,
                hints=None, context_level=None):
        """context_level: 'minimal' (parent source only) or 'rich' (source +
        rendered diagnostics + inspirations). hints: extra strategy lines
        appended to the system prompt (meta-prompt co-evolution)."""
        level = context_level or self.context_level
        system = self.task_system if self.task_system else self.BASE_SYSTEM
        # engineering rules ride along mechanically (see ENGINEERING_RULES note)
        system += self.ENGINEERING_RULES
        if hints:
            system += "\n\nAdditional strategy hints to follow:\n- " + \
                      "\n- ".join(hints)
        # Prompt cap (token-economy point 9): the FULL parent stays the diff
        # base below; only the listing shown to the model is bounded.
        parent_for_prompt = _cap_source(parent_source)
        capped = len(parent_for_prompt) < len(parent_source)
        msg = []
        if level == "minimal":
            msg.append(f"Current program:\n```python\n{parent_for_prompt}\n```")
        else:
            msg.append("Current program and its diagnostics on real held-out "
                       f"galaxies:\n{self._render_diagnostics(parent_metrics)}\n"
                       f"```python\n{parent_for_prompt}\n```")
            if inspirations:
                joined = "\n\n---\n\n".join(
                    _cap_source(i, budget=1500) for i in inspirations)
                msg.append("Inspirations (other, diverse approaches — borrow ideas):\n"
                           f"```python\n{joined}\n```")
        msg.append("Propose an improvement that lowers sigma_NMAD and eta on real "
                   "held-out data. Keep it correct.")
        # Point 11 (2026-10-10): the first block (program + diagnostics) is
        # IDENTICAL across the retry loop and the step loop of one episode, so
        # mark it cacheable — the proxy honours cache_control (verified: repeat
        # calls came back cache_read_input_tokens>0 at ~1/10 input cost).
        blocks = [{"type": "text", "text": msg[0],
                   "cache_control": {"type": "ephemeral"}}]
        blocks += [{"type": "text", "text": m} for m in msg[1:]]
        try:
            self.n_calls += 1
            r = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                system=system, messages=[{"role": "user",
                                          "content": blocks}])
            text = "".join(b.text for b in r.content if hasattr(b, "text"))
            usage = {"input_tokens": int(getattr(r.usage, "input_tokens", 0) or 0),
                     "output_tokens": int(getattr(r.usage, "output_tokens", 0) or 0)}
            # point 7: raw-client calls bypass the gateway's recorder — record here
            from ._llm import record_usage as _record_usage
            _record_usage("proposer", self.model, usage)
            child, mode = apply_diff(parent_source, text, self.entry_point)
            if mode == "unchanged":
                self.n_parse_failures += 1
                self._dump_parse_failure(text)
            # residual-defect audit (2026-10-20): after a diff applied, count
            # applied SEARCH blocks whose OLD text still occurs verbatim in the
            # child — the model judged that code defective but identical copies
            # survive at other call sites (the "too-surgical patch" failure
            # mode). Report-only: the child still goes to EVALUATE unchanged;
            # a residual count is a signal for the next proposal round, never
            # grounds to alter the child here.
            n_residual = 0
            if mode.startswith("diff"):
                for old, _new in _SEARCH_RE.findall(text):
                    old_s = old.strip("\n")
                    if old_s and old_s in child:
                        n_residual += 1
            return child, None, {"mode": mode, "raw_len": len(text),
                                 "n_residual_defects": n_residual,
                                 "model": self.model,
                                 "in": usage["input_tokens"],
                                 "out": usage["output_tokens"],
                                 "capped_parent": capped,
                                 "parse_failures": self.n_parse_failures}
        except Exception as e:
            self.n_errors += 1
            return None, None, {"error": f"{type(e).__name__}: {str(e)[:120]}",
                                "model": self.model}


class GeneticProposer(Proposer):
    """Offline fallback. Mutates a structured spec and re-renders source."""
    name = "genetic"

    def __init__(self, seed=0):
        import numpy as np
        self.rng = np.random.default_rng(seed)
        self.n_calls = 0

    def _mutate_spec(self, spec):
        s = copy.deepcopy(spec)
        move = self.rng.choice(["color", "model", "scale", "degree", "alpha",
                                "trees", "depth", "include_r"])
        if move == "color":
            cur = list(s["color_pairs"])
            if cur and self.rng.random() < 0.5:
                del cur[self.rng.integers(0, len(cur))]
            else:
                missing = [p for p in ALL_PAIRS if p not in cur]
                if missing:
                    cur.append(str(self.rng.choice(missing)))
            s["color_pairs"] = cur
        elif move == "model":
            s["model"] = str(self.rng.choice(["linear", "ridge", "rf"]))
        elif move == "scale":
            s["scale"] = str(self.rng.choice(["none", "standard", "robust"]))
        elif move == "degree":
            s["degree"] = int(self.rng.choice([1, 2, 3]))
        elif move == "alpha":
            s["alpha"] = float(10 ** self.rng.uniform(-3, 3))
        elif move == "trees":
            s["rf_trees"] = int(self.rng.integers(20, 201))
        elif move == "depth":
            s["rf_depth"] = int(self.rng.integers(3, 16))
        elif move == "include_r":
            s["include_r"] = not bool(s.get("include_r", False))
        # guarantee >=1 feature
        if not s["color_pairs"] and not s.get("include_r"):
            s["include_r"] = True
        return s

    def propose(self, parent_source, parent_metrics, parent_spec, inspirations):
        self.n_calls += 1
        spec = parent_spec if parent_spec else copy.deepcopy(NAIVE_SPEC)
        child_spec = self._mutate_spec(spec)
        return render_source(child_spec), child_spec, {"mode": "genetic"}
