"""test_engineering_rules.py — the 2026-10-20 engineering-rules package
(adapted from the Ponytail/Karpathy ruleset review):

 1. ENGINEERING_RULES appended mechanically to every proposer system prompt
    (incl. task_system variants) — rules in a folder activate ~never, so this
    is code-enforced, not model-discretionary.
 2. Residual-defect audit: after a diff applies, SEARCH blocks whose OLD text
    still occurs verbatim in the child are counted in info['n_residual_defects']
    (report-only; child is never altered).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scientific_discovery"))

from evolved_analysis import proposer as proposer_mod  # noqa: E402
from evolved_analysis.proposer import LLMProposer      # noqa: E402

RESULTS = []


def test_engineering_rules_in_base_system():
    assert "Reuse before you build" in LLMProposer.BASE_SYSTEM + LLMProposer.ENGINEERING_RULES
    assert "Never simplify away" in LLMProposer.ENGINEERING_RULES
    assert "Fix the root, not the symptom" in LLMProposer.ENGINEERING_RULES
    RESULTS.append(("rules present in prompt material", True))


def test_rules_appended_to_task_system_variants():
    # a custom task_system must ALSO get the rules (mechanical append in propose())
    ts = "You are a custom task system."
    p = LLMProposer.__new__(LLMProposer)   # no gateway init; only prompt assembly
    p.task_system = ts
    p.context_level = "rich"
    p.entry_point = "run_claim"
    p.n_calls = p.n_errors = p.n_parse_failures = 0
    p.max_tokens = 100
    p._failure_dir = None
    p.model = "stub"
    p.client = None

    captured = {}

    class _FakeContent:
        text = "```python\n" + _CHILD + "```"

    class _FakeResp:
        content = [_FakeContent()]
        usage = type("u", (), {"input_tokens": 1, "output_tokens": 1})()

    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return _FakeResp()

    p.client = _FakeClient()
    from evolved_analysis import _llm
    _llm.record_usage = lambda *a, **k: None
    p.propose(_PARENT, {}, None, [])
    assert captured["system"].startswith(ts), "custom task_system must stay the base"
    assert "ENGINEERING RULES" in captured["system"], \
        "rules must ride along with task_system variants"


_PARENT = (
    'CLAIM = "test claim"\n\n'
    "def helper(df):\n"
    "    return df['x'] * 2  # buggy scaling\n\n"
    "def run_claim(df_train, df_eval):\n"
    "    a = helper(df_train)\n"
    "    b = helper(df_eval)\n"
    "    return {'effect': 0.1, 'pvalue': 0.5}\n"
)
_CHILD = _PARENT.replace("* 2", "* 3")


def test_residual_defect_counted_when_shared_bug_patched_once():
    # the model patches the helper in ONE of its two usages (too-surgical
    # patch); the patched old text still occurs at the other call site
    response = (
        "<<<SEARCH>>>\n    a = helper(df_train)\n<<<REPLACE>>>\n"
        "    a = df_train['x'] * 3\n<<<END>>>\n"
    )
    # parent has the searched line once -> applies cleanly, no residual
    child, mode = proposer_mod.apply_diff(_PARENT, response, "run_claim")
    assert mode == "diff"
    # residual detection mirrors the proposer's inline audit
    import re
    n_residual = sum(
        1 for old, _ in re.findall(
            r"<<<\s*SEARCH\s*>>>(.*?)<<<\s*REPLACE\s*>>>(.*?)<<<\s*END\s*>>>",
            response, re.DOTALL)
        if old.strip("\n") and old.strip("\n") in child)
    assert n_residual == 0
    RESULTS.append(("clean single-site patch -> 0 residuals", True))


def test_residual_defect_detected_for_duplicate_defect():
    parent = (
        "def run_claim(df_train, df_eval):\n"
        "    a = clip(df_train) * 1.0  # wrong factor\n"
        "    b = clip(df_eval) * 1.0  # wrong factor\n"
        "    return {'effect': a, 'pvalue': b}\n"
    )
    response = (
        "<<<SEARCH>>>\n* 1.0  # wrong factor\n<<<REPLACE>>>\n* 2.0\n<<<END>>>\n"
    )
    child, mode = proposer_mod.apply_diff(parent, response, "run_claim")
    assert mode == "diff"          # replace(…, 1) applied the first occurrence
    assert "* 1.0  # wrong factor" in child   # identical defect survives at site 2
    import re
    n_residual = sum(
        1 for old, _ in re.findall(
            r"<<<\s*SEARCH\s*>>>(.*?)<<<\s*REPLACE\s*>>>(.*?)<<<\s*END\s*>>>",
            response, re.DOTALL)
        if old.strip("\n") and old.strip("\n") in child)
    assert n_residual == 1, "surviving copy of patched-defect text must count"
    RESULTS.append(("duplicate defect partially patched -> 1 residual", True))


def teardown_module(module):
    print("\n=== engineering-rules tests ===")
    for name, ok in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
