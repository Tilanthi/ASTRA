"""Tests for the token-economy package (v14.20, 2026-10-10).

Pins the code-side behaviours of the 12-point token-usage remediation:
  * point 2  — supervisor honours ASTRA_DISCOVERY_EVOLUTION_DISABLED from the
               llm_env FILE (launchd does not inherit shell env vars — the same
               rule the module already applies to ASTRA_EVOLUTION_MODULE);
  * point 6  — apply_diff tolerance (a failed SEARCH block no longer discards a
               valid rewrite later in the response; bare ``` fences parse) and
               the deterministic df_eval -> df_train rewrite before re-proposing;
  * point 7  — proposer + meta-prompt usage routed to the token ledger; the
               daily budget circuit breaker;
  * point 8  — the near-dup novelty tier: a cache-miss claim within tau of a
               fresh cached KNOWN verdict short-circuits to "known-precheck"
               (suppression-only, never toward novel, ledger-logged, env kill);
  * point 9  — Gate-2 judge model override (cheap FAST model by default) and
               the capped parent source in the proposer prompt;
  * point 10 — seed sanity once per code version (marker file; cached seed row
               preserves episode splitting in predictions.py);
  * point 12 — count sidecar instead of the 43.6 MB novelty-cache parse;
               lifetime-trial sidecar; tail reads for the verdict log; novelty
               cache prune + retrieved-strip; store mtime short-circuit in the
               consumer; rotation caps for the unbounded ledgers.

All tests run against a tempdir: the real ~/.astra_persistent stores are never
touched.

Run: python3 astra_core/tests/test_token_economy.py
"""
import hashlib
import importlib.util as _ilu
import json
import os
import re as _re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Decoupled imports — exactly how production runs these modules
# (PYTHONPATH=astra_core/scientific_discovery, no astra_core/__init__).
SD = REPO / "astra_core" / "scientific_discovery"
sys.path.insert(0, str(SD))

from evolved_analysis import (  # noqa: E402
    proposer as proposer_mod,
    run_claim_search as rcs,
    novelty_gate as ng,
    novelty_precheck as npc,
    claim_gates as cg,
    evidence_ledger as el,
    predictions as pred,
    data_lake as dl,
    meta_prompt as mp,
    _llm,
)
import evolved_discovery_consumer as edc  # noqa: E402

RESULTS = []


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
class _Env:
    """Set env vars for the duration, restore after."""

    def __init__(self, **kw):
        self.kw = kw
        self.orig = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.orig[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, v in self.orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class _Tmp:
    def __init__(self, prefix="token_economy_"):
        self.tmp = Path(tempfile.mkdtemp(prefix=prefix))

    def __enter__(self):
        return self.tmp

    def __exit__(self, *exc):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fake_embedder_module(target_module):
    """Deterministic word-hash embedder standing in for MiniLM (no download,
    no salted hash): 64 dims, L2-normalised term counts."""
    import numpy as np

    class _Fake:
        def encode(self, texts, normalize_embeddings=False):
            out = []
            for t in texts:
                v = np.zeros(64)
                for w in _re.findall(r"[a-z0-9]+", str(t).lower()):
                    v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
                n = float(np.linalg.norm(v))
                out.append(v / n if n else v)
            return np.array(out)

    target_module._MODEL = _Fake()


# --------------------------------------------------------------------------- #
# point 2 — supervisor disable flag from the llm_env file                     #
# --------------------------------------------------------------------------- #
def _load_supervisor():
    spec = _ilu.spec_from_file_location(
        "astra_supervisor_under_test",
        REPO / "astra_core" / "autonomous_discovery_supervisor.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_supervisor_disable_flag_read_from_llm_env_file():
    sup = _load_supervisor()

    with _Tmp() as tmp, _Env(ASTRA_DISCOVERY_EVOLUTION_DISABLED=None,
                             ANTHROPIC_AUTH_TOKEN="test-token"):
        envfile = tmp / "llm_env"
        sup.LLM_ENV_FILE = envfile

        class _Sup(sup.AutonomousDiscoverySupervisor):
            def _hydrate(self):
                pass

        # control: flag in NEITHER env nor file -> evolution enabled
        envfile.write_text("ANTHROPIC_AUTH_TOKEN=test-token\n")
        s = _Sup()
        assert s._evolution_disabled is False, "control case wrongly disabled"

        # file-only flag (the launchd case) -> disabled
        envfile.write_text("ANTHROPIC_AUTH_TOKEN=test-token\n"
                           "ASTRA_DISCOVERY_EVOLUTION_DISABLED=1\n")
        s2 = _Sup()
        assert s2._evolution_disabled is True, \
            "llm_env-file flag ignored (launchd would keep evolving)"

        # env var still works and wins
        with _Env(ASTRA_DISCOVERY_EVOLUTION_DISABLED="0"):
            s3 = _Sup()
            assert s3._evolution_disabled is False
        sup.LLM_ENV_FILE = Path.home() / ".astra_persistent" / "llm_env"
    RESULTS.append(("point2 supervisor llm_env disable", True))


# --------------------------------------------------------------------------- #
# point 6a — apply_diff tolerance                                              #
# --------------------------------------------------------------------------- #
_PARENT = (
    'CLAIM = "test claim"\n\n'
    "def run_claim(df_train, df_eval):\n"
    "    df = df_train\n"
    "    return {'effect': 0.1, 'pvalue': 0.5}\n"
)
_CHILD_REWRITE = (
    'CLAIM = "improved claim"\n\n'
    "def run_claim(df_train, df_eval):\n"
    "    df = df_train\n"
    "    return {'effect': 0.2, 'pvalue': 0.01}"
)


def test_apply_diff_failed_block_falls_through_to_rewrite():
    response = (
        "<<<SEARCH>>>\nTHIS TEXT IS NOT IN THE PARENT\n<<<REPLACE>>>\nx\n<<<END>>>\n"
        "```python\n" + _CHILD_REWRITE + "```\n"
    )
    child, mode = proposer_mod.apply_diff(_PARENT, response, "run_claim")
    assert child == _CHILD_REWRITE, \
        f"valid rewrite after a failed block was discarded (mode={mode})"
    assert mode == "rewrite"
    RESULTS.append(("point6a failed block -> rewrite", True))


def test_apply_diff_partial_blocks_apply_what_matched():
    response = (
        "<<<SEARCH>>>\n    return {'effect': 0.1, 'pvalue': 0.5}\n"
        "<<<REPLACE>>>\n    return {'effect': 0.3, 'pvalue': 0.02}\n<<<END>>>\n"
        "<<<SEARCH>>>\nABSENT LINE\n<<<REPLACE>>>\ny\n<<<END>>>\n"
    )
    child, mode = proposer_mod.apply_diff(_PARENT, response, "run_claim")
    assert mode == "diff-partial", f"expected partial-diff mode, got {mode}"
    assert "'effect': 0.3" in child and "'effect': 0.1" not in child
    RESULTS.append(("point6a partial diff applies matched blocks", True))


def test_extract_python_block_accepts_bare_fence():
    assert proposer_mod.extract_python_block("```\nx = 1\n```") == "x = 1", \
        "bare ``` fence (no language tag) failed to parse"
    RESULTS.append(("point6a bare fence", True))


# --------------------------------------------------------------------------- #
# point 6b — deterministic df_eval -> df_train rewrite                         #
# --------------------------------------------------------------------------- #
def test_rewrite_eval_to_train():
    bad = ("def run_claim(df_train, df_eval):\n"
           "    df = df_eval\n"
           "    return {'effect': 0.1}\n")
    ok, why = cg.claim_uses_train_split(bad)
    assert not ok
    fixed = rcs._rewrite_eval_to_train(bad)
    ok2, _ = cg.claim_uses_train_split(fixed)
    assert ok2, f"rewrite did not make the candidate train-computing: {fixed}"
    assert "def run_claim(df_train, df_eval)" in fixed, \
        "signature line must keep BOTH parameters"
    # already-correct source passes through untouched
    good = bad.replace("df_eval", "df_train")
    assert rcs._rewrite_eval_to_train(good) == good
    RESULTS.append(("point6b df_eval->df_train rewrite", True))


# --------------------------------------------------------------------------- #
# point 7 — usage recording + budget breaker                                   #
# --------------------------------------------------------------------------- #
class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeUsage:
    input_tokens = 111
    output_tokens = 22


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return _FakeResponse(self.text)


class _FakeClient:
    """Real-shape Anthropic stand-in: production code calls
    ``client.messages.create(...)``, so the fake must nest .messages."""

    def __init__(self, text):
        self.messages = _FakeMessages(text)


def _fresh_proposer(tmp):
    """LLMProposer without touching the network: fake client."""
    p = object.__new__(proposer_mod.LLMProposer)
    p._gw = None
    p.client = _FakeClient("```python\n" + _CHILD_REWRITE + "```\n")
    p.model = "test-model"
    p.max_tokens = 1600
    p.timeout = 90.0
    p.context_level = "rich"
    p.task_system = None
    p.entry_point = "run_claim"
    p.n_calls = 0
    p.n_errors = 0
    p.n_parse_failures = 0
    p._failure_dir = tmp / "proposer_failures"
    return p


def test_proposer_records_usage_and_caps_source():
    with _Tmp() as tmp, _Env(ASTRA_TOKEN_LEDGER=str(tmp / "ledger.jsonl")):
        recorded = []
        _llm.record_usage = (lambda caller, model, usage, ledger_path=None:
                             recorded.append((caller, model, dict(usage))))
        p = _fresh_proposer(tmp)
        long_parent = _PARENT + "# " + "x" * 40000 + "\n"
        child, _spec, info = p.propose(
            long_parent, {"sigma_nmad": 0.05, "eta": 0.05},
            None, ["insp " + "y" * 5000])
        assert child == _CHILD_REWRITE
        assert recorded and recorded[0][0] == "proposer", \
            "proposer LLM usage not ledgered"
        assert recorded[0][2] == {"input_tokens": 111, "output_tokens": 22}
        sent = p.client.messages.calls[0]
        blob = sent["messages"][0]["content"]
        assert len(blob) < 20000, \
            f"capped prompt still huge ({len(blob)} chars)"
        assert info.get("capped_parent") is True
    RESULTS.append(("point7 proposer usage + source cap", True))


def test_proposer_dumps_parse_failures():
    with _Tmp() as tmp:
        _llm.record_usage = lambda *a, **k: None
        p = _fresh_proposer(tmp)
        p.client = _FakeClient("sorry I cannot produce that")
        child, _spec, info = p.propose(_PARENT, {}, None, [])
        assert child == _PARENT and info.get("mode") == "unchanged"
        dumps = list(p._failure_dir.glob("*.txt"))
        assert dumps, "no parse-failure dump written for an unchanged proposal"
        assert "sorry I cannot" in dumps[0].read_text()
        assert info.get("parse_failures") == 1
    RESULTS.append(("point7 parse-failure capture", True))


def test_proposer_marks_parent_block_cacheable():
    """Point 11: the z.ai proxy honours cache_control (verified 2026-10-10:
    identical second call returned cache_read_input_tokens=1280 with input
    1310->30), and _evolve_steps re-sends the SAME parent block up to
    steps x retries times per episode. Marking the stable first user block
    cacheable turns every repeat into a cached read."""
    with _Tmp() as tmp:
        _llm.record_usage = lambda *a, **k: None
        p = _fresh_proposer(tmp)
        p.propose(_PARENT, {"sigma_nmad": 0.05, "eta": 0.05}, None, [])
        sent = p.client.messages.calls[0]
        content = sent["messages"][0]["content"]
        assert isinstance(content, list), \
            "user content must be block-list form for cache_control"
        first = content[0]
        assert first.get("type") == "text" and first.get("cache_control") == \
            {"type": "ephemeral"}, "stable first block not marked cacheable"
        assert "```python" in first.get("text", ""), \
            "cacheable block should carry the parent listing"
        assert any("Propose an improvement" in b.get("text", "")
                   for b in content), "tail instruction block missing"
    RESULTS.append(("point11 proposer cache_control wiring", True))


def test_meta_prompt_records_usage():
    with _Tmp() as tmp, _Env(ASTRA_TOKEN_LEDGER=str(tmp / "ledger.jsonl")):
        recorded = []
        _llm.record_usage = (lambda caller, model, usage, ledger_path=None:
                             recorded.append((caller, model, dict(usage))))

        class _P:
            client = _FakeClient("A single concrete hint.")
            model = "test-model"

        pop = mp.HintSetPopulation(proposer=_P(), evolve_every=1)
        pop._since_evolve = 1
        hint = pop._llm_propose_hint(pop.sets[0])
        assert hint == "A single concrete hint."
        assert recorded and recorded[0][0] == "meta_prompt", \
            "meta-prompt usage not ledgered"
    RESULTS.append(("point7 meta_prompt usage", True))


def _load_token_ledger():
    spec = _ilu.spec_from_file_location(
        "token_ledger_under_test",
        REPO / "astra_core" / "intelligence" / "token_ledger.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_today_total_and_rotation():
    tl = _load_token_ledger()
    with _Tmp() as tmp:
        ledger = tmp / "token_ledger.jsonl"
        today = time.strftime("%Y-%m-%d")
        _write_jsonl(ledger, [
            {"ts": "2020-01-01T00:00:00", "caller": "x", "model": "m",
             "input_tokens": 999999, "output_tokens": 1},
            {"ts": today + "T09:00:00", "caller": "x", "model": "m",
             "input_tokens": 100, "output_tokens": 50},
            {"ts": today + "T10:00:00", "caller": "y", "model": "m",
             "input_tokens": 1, "output_tokens": 2},
        ])
        tt = tl.today_total(ledger)
        assert tt == {"input_tokens": 101, "output_tokens": 52, "calls": 2}, tt
        # rotation: cap tiny -> next append rotates to .1
        cap_orig = getattr(tl, "LEDGER_CAP_BYTES", None)
        tl.LEDGER_CAP_BYTES = 10
        try:
            tl.record_usage("x", "m", {"input_tokens": 1, "output_tokens": 1},
                            ledger_path=ledger)
            assert (tmp / "token_ledger.jsonl.1").exists(), "ledger not rotated"
        finally:
            if cap_orig is not None:
                tl.LEDGER_CAP_BYTES = cap_orig
    RESULTS.append(("point7 today_total + ledger rotation", True))


def test_budget_breaker():
    with _Tmp() as tmp:
        ledger = tmp / "token_ledger.jsonl"
        today = time.strftime("%Y-%m-%d")
        _write_jsonl(ledger, [
            {"ts": today + "T09:00:00", "caller": "x", "model": "m",
             "input_tokens": 2_400_000, "output_tokens": 100},
        ])
        with _Env(ASTRA_TOKEN_LEDGER=str(ledger),
                  ASTRA_DAILY_TOKEN_BUDGET=None):
            exceeded, detail = rcs._budget_state()
            assert exceeded is True, \
                "2.4M tokens today must trip the default 2M budget"
        with _Env(ASTRA_TOKEN_LEDGER=str(ledger),
                  ASTRA_DAILY_TOKEN_BUDGET="0"):
            exceeded, _ = rcs._budget_state()
            assert exceeded is False, "budget 0 must disable the breaker"
        with _Env(ASTRA_TOKEN_LEDGER=str(ledger),
                  ASTRA_DAILY_TOKEN_BUDGET="5000000"):
            exceeded, _ = rcs._budget_state()
            assert exceeded is False
    RESULTS.append(("point7 daily budget breaker", True))


def test_llm_record_usage_decoupled():
    with _Tmp() as tmp, _Env(ASTRA_TOKEN_LEDGER=str(tmp / "l.jsonl")):
        _real = _llm.record_usage
        _llm.record_usage = _load_token_ledger().record_usage
        try:
            _llm.record_usage("caller-x", "model-y",
                              {"input_tokens": 7, "output_tokens": 3})
            rows = [json.loads(l) for l in
                    (tmp / "l.jsonl").read_text().splitlines()]
            assert rows[-1]["caller"] == "caller-x" and \
                rows[-1]["input_tokens"] == 7
        finally:
            _llm.record_usage = _real
    RESULTS.append(("point7 decoupled recorder", True))


# --------------------------------------------------------------------------- #
# point 8 — near-dup novelty tier                                              #
# --------------------------------------------------------------------------- #
def test_near_dup_match_thresholds():
    _fake_embedder_module(npc)
    A = "alpha beta gamma delta epsilon zeta"
    B = "alpha beta gamma delta epsilon zeta eta"     # near-dup, cos ~0.926
    C = "theta iota kappa lambda mu nu"               # unrelated
    key, sim = npc.near_dup_match(A, {"k1": B, "k2": C})
    assert key == "k1" and sim >= 0.90, f"near-dup missed (key={key}, sim={sim})"
    key2, sim2 = npc.near_dup_match(C, {"k1": B})
    assert key2 is None and sim2 < 0.90
    npc._MODEL = None
    RESULTS.append(("point8 near_dup_match thresholds", True))


def test_dedup_tau_env_control():
    with _Env(ASTRA_NOVELTY_DEDUP_TAU=None):
        assert npc.dedup_tau() == npc.DEDUP_TAU_DEFAULT == 0.90
    with _Env(ASTRA_NOVELTY_DEDUP_TAU="0"):
        assert npc.dedup_tau() <= 0, "env 0 must disable the tier"
    with _Env(ASTRA_NOVELTY_DEDUP_TAU="0.97"):
        assert npc.dedup_tau() == 0.97
    RESULTS.append(("point8 tau env control", True))


class _IsolatedNovelty:
    """Redirect novelty-gate (+ precheck log) paths to a tempdir."""

    def __init__(self, cache=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="novelty_test_"))
        self.paths = {
            "CACHE_PATH": self.tmp / "novelty_cache.json",
            "CACHE_COUNT_PATH": self.tmp / "novelty_cache.count",
        }
        self.orig = {k: getattr(ng, k) for k in self.paths}
        self.orig_pc = npc.PRECHECK_PATH
        npc.PRECHECK_PATH = self.tmp / "novelty_precheck.jsonl"
        # Write the fixture to the PATCHED path (self.paths), never to the
        # live ng.CACHE_PATH — __init__ runs before __enter__ redirects it,
        # and a write here once clobbered the real 43.6 MB cache (2026-10-09).
        if cache is not None:
            self.paths["CACHE_PATH"].write_text(json.dumps(cache))

    def __enter__(self):
        for k, v in self.paths.items():
            setattr(ng, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.orig.items():
            setattr(ng, k, v)
        npc.PRECHECK_PATH = self.orig_pc
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def test_known_precheck_short_circuit():
    with _Tmp() as tmp, _Env(ASTRA_NOVELTY_DEDUP_TAU=None):
        _fake_embedder_module(npc)
        prior_text = "alpha beta gamma delta epsilon zeta"
        near_text = "alpha beta gamma delta epsilon zeta eta"
        cache = {
            "deadbeef": {"novel": False, "status": "known",
                         "claim": prior_text,
                         "live_checked_ts": time.time(), "n_retrieved": 5},
        }
        with _IsolatedNovelty(cache) as iso:
            def _boom(*a, **k):
                raise AssertionError("retrieval must not run on a dedup hit")
            ng._retrieve_papers = _boom
            res = ng.check_novelty(near_text, use_s2=False)
            assert res.status == "known-precheck", res.status
            assert res.novel is False and res.from_cache is True
            assert res.n_retrieved == 0
            # logged for the standing precision measurement
            rows = [json.loads(l) for l in npc.PRECHECK_PATH.read_text()
                    .splitlines()]
            assert any(r.get("verdict") == "known-precheck" for r in rows)
            # the dup claim got a compact cache entry of its own
            c2 = json.loads(ng.CACHE_PATH.read_text())
            dup_keys = [k for k, v in c2.items()
                        if v.get("status") == "known-precheck"]
            assert dup_keys and "retrieved" not in c2[dup_keys[0]]
        npc._MODEL = None
    RESULTS.append(("point8 known-precheck short-circuit", True))


def test_known_precheck_respects_kill_switch_and_stale_priors():
    with _Tmp():
        _fake_embedder_module(npc)
        prior_text = "alpha beta gamma delta epsilon zeta"
        near_text = "alpha beta gamma delta epsilon zeta eta"
        # stale prior (live check older than TTL) must NOT be reused
        stale = {"aaaa": {"novel": False, "status": "known",
                          "claim": prior_text,
                          "live_checked_ts": time.time() - 10 * 7 * 24 * 3600}}
        def _boom(*a, **k):
            raise AssertionError("stale-prior case reached retrieval unexpectedly")
        with _IsolatedNovelty(dict(stale)):
            ng._retrieve_papers = _boom
            res = ng.check_novelty(near_text, use_s2=False)
            assert res.status != "known", "stale prior must not resolve to known"
        # kill switch: tier off -> dedup never fires (falls to retrieval path;
        # here retrieval is unavailable so the honest status is retrieval-failed)
        fresh = {"bbbb": {"novel": False, "status": "known",
                          "claim": prior_text, "live_checked_ts": time.time()}}
        with _Env(ASTRA_NOVELTY_DEDUP_TAU="0"):
            with _IsolatedNovelty(dict(fresh)):
                ng._retrieve_papers = lambda *a, **k: []
                res = ng.check_novelty(near_text, use_s2=False)
                assert res.status == "retrieval-failed", \
                    "kill switch did not disable the dedup tier"
        npc._MODEL = None
    RESULTS.append(("point8 kill switch + stale priors", True))


# --------------------------------------------------------------------------- #
# point 9 — cheap judge model                                                  #
# --------------------------------------------------------------------------- #
def test_judge_model_default_and_env():
    with _Env(ASTRA_JUDGE_MODEL=None):
        assert ng._judge_model() == "claude-haiku-4-5-20251001", \
            "Gate-2 judge must default to the cheap FAST model"
    with _Env(ASTRA_JUDGE_MODEL="some-other-model"):
        assert ng._judge_model() == "some-other-model"
    RESULTS.append(("point9 judge model default/env", True))


def test_judge_known_passes_model():
    captured = {}

    class _FakeGW:
        def complete(self, system, messages, model=None, max_tokens=None,
                     caller=None, ledger_path=None):
            captured.update(model=model, caller=caller)
            return ('{"known": true, "reason": "entailed", "by_abstract": 0,'
                    ' "confidence": 0.9, "reasoning": "x"}', {})

    papers = [ng.Paper("arxiv", "T", "A", "id1", "2020")]
    gw_orig, ng._GATEWAY = ng._GATEWAY, _FakeGW()
    try:
        known, ent, label, reasoning, conf = ng._judge_known("claim", papers)
        assert known is True
        assert captured["model"] == "claude-haiku-4-5-20251001"
        assert captured["caller"] == "novelty_gate"
    finally:
        ng._GATEWAY = gw_orig
    RESULTS.append(("point9 judge call wires model+caller", True))


def test_cap_source():
    big = "head\n" + "x" * 40000 + "\ntail_marker_line\n"
    capped = proposer_mod._cap_source(big, budget=3000)
    assert len(capped) <= 3100, f"cap not enforced ({len(capped)})"
    assert "head" in capped and "tail_marker_line" in capped
    assert "... [" in capped, "elision marker missing"
    assert proposer_mod._cap_source("short", budget=3000) == "short"
    RESULTS.append(("point9 _cap_source", True))


# --------------------------------------------------------------------------- #
# point 10 — seed sanity once per code version                                 #
# --------------------------------------------------------------------------- #
class _IsolatedSeed:
    def __init__(self, tmp):
        self.orig = {
            "SEED_SANITY_MARKER": getattr(rcs, "SEED_SANITY_MARKER",
                                           tmp / "seed_sanity.json"),
            "VERDICT_LOG": rcs.VERDICT_LOG,
        }
        rcs.SEED_SANITY_MARKER = tmp / "seed_sanity.json"
        rcs.VERDICT_LOG = tmp / "claim_verdicts.jsonl"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for k, v in self.orig.items():
            setattr(rcs, k, v)
        return False


def test_seed_sanity_marker_lifecycle():
    with _Tmp() as tmp, _IsolatedSeed(tmp):
        sig = "testsig123"
        assert rcs._seed_sanity_needed(sig) is True, \
            "no marker -> seed must run"
        verdict = {
            "claim": "seed claim", "dataset": "legacy",
            "program_hash": "abc", "both_pass": False,
            "gate1": {"pass": True, "reason": "ok",
                      "metrics": {"effect": 0.5, "pvalue": 1e-40}},
            "gate2": {"pass": False, "status": "known"},
        }
        rcs._write_seed_marker(sig, verdict)
        assert rcs._seed_sanity_needed(sig) is False, \
            "fresh marker for this signature -> seed must NOT re-run"
        assert rcs._seed_sanity_needed("different-sig") is True, \
            "code changed -> seed must re-run"
        row = rcs._synthetic_seed_verdict(sig)
        assert row is not None
        assert row["label"] == "seed" and row.get("seed_cached") is True
        assert row["gate1"]["pass"] is True
        assert row["gate2"]["status"] == "known"
        # a failed seed must never be cached
        rcs._write_seed_marker("failsig", {"gate1": {"pass": False}})
        assert rcs._seed_sanity_needed("failsig") is True
    RESULTS.append(("point10 seed marker lifecycle", True))


def test_seed_signature_changes_with_code():
    s1 = rcs._seed_signature()
    assert isinstance(s1, str) and len(s1) >= 8
    # signature covers the seed source itself
    from evolved_analysis import claim_task as ct
    orig = ct.NAIVE_CLAIM_SEED
    ct.NAIVE_CLAIM_SEED = orig + "#x"
    try:
        assert rcs._seed_signature() != s1, \
            "seed signature must change when the seed changes"
    finally:
        ct.NAIVE_CLAIM_SEED = orig
    RESULTS.append(("point10 seed signature covers code", True))


# --------------------------------------------------------------------------- #
# point 12 — I/O: sidecars, tails, prune, mtime, rotation                      #
# --------------------------------------------------------------------------- #
def test_novelty_cache_size_sidecar():
    with _Tmp() as tmp:
        cache = tmp / "novelty_cache.json"
        cache.write_text(json.dumps({f"k{i}": {"claim": f"c{i}"}
                                     for i in range(3)}))
        sidecar = tmp / "novelty_cache.count"
        orig = {"NOVELTY_CACHE": cg.NOVELTY_CACHE,
                "NOVELTY_CACHE_COUNT": getattr(cg, "NOVELTY_CACHE_COUNT",
                                               tmp / "novelty_cache.count")}
        cg.NOVELTY_CACHE, cg.NOVELTY_CACHE_COUNT = cache, sidecar
        try:
            # stale/absent sidecar -> counted from the file, sidecar written
            n = cg._novelty_cache_size()
            assert n == 3
            assert sidecar.exists()
            # fresh sidecar wins WITHOUT parsing the cache file
            sidecar.write_text(json.dumps({"n": 777}))
            os.utime(sidecar,
                     (time.time() + 5, time.time() + 5))  # newer than cache
            assert cg._novelty_cache_size() == 777
            # stale sidecar (cache newer) -> re-count
            cache.write_text(json.dumps({f"k{i}": {} for i in range(5)}))
            os.utime(cache, (time.time() + 10, time.time() + 10))
            assert cg._novelty_cache_size() == 5
        finally:
            cg.NOVELTY_CACHE = orig["NOVELTY_CACHE"]
            cg.NOVELTY_CACHE_COUNT = orig["NOVELTY_CACHE_COUNT"]
    RESULTS.append(("point12 novelty count sidecar", True))


def test_save_cache_prunes_and_strips_retrieved():
    with _Tmp() as tmp:
        # both paths to the tempdir — a getattr passthrough here once pointed
        # the "patched" count sidecar at the REAL store (trap #S37, variant 3)
        orig = {"CACHE_PATH": ng.CACHE_PATH,
                "CACHE_COUNT_PATH": ng.CACHE_COUNT_PATH}
        ng.CACHE_PATH = tmp / "novelty_cache.json"
        ng.CACHE_COUNT_PATH = tmp / "novelty_cache.count"
        max_orig = getattr(ng, "NOVELTY_CACHE_MAX_ENTRIES", None)
        ng.NOVELTY_CACHE_MAX_ENTRIES = 5
        try:
            cache = {}
            for i in range(10):
                cache[f"k{i}"] = {"status": "known", "claim": f"c{i}",
                                  "retrieved": [{"title": "t", "abstract":
                                                 "a" * 900}] * 8}
            ng._save_cache(cache)
            on_disk = json.loads(ng.CACHE_PATH.read_text())
            assert len(on_disk) == 5, f"prune to max failed ({len(on_disk)})"
            kept = set(on_disk)
            assert kept == {f"k{i}" for i in range(5, 10)}, \
                "prune must keep the NEWEST entries"
            assert all("retrieved" not in v for v in on_disk.values()), \
                "full retrieved abstracts still persisted (the 43 MB bloat)"
            assert json.loads(ng.CACHE_COUNT_PATH.read_text())["n"] == 5
        finally:
            ng.CACHE_PATH = orig["CACHE_PATH"]
            ng.CACHE_COUNT_PATH = orig["CACHE_COUNT_PATH"]
            if max_orig is not None:
                ng.NOVELTY_CACHE_MAX_ENTRIES = max_orig
    RESULTS.append(("point12 cache prune + strip + count", True))


class _IsolatedLedger:
    def __init__(self, verdict_rows=None, ledger_rows=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="evidence_test_"))
        self.paths = {"EVOLVED_PROGRAMS": self.tmp,
                      "LEDGER": self.tmp / "evidence_ledger.jsonl",
                      "VERDICT_LOG": self.tmp / "claim_verdicts.jsonl",
                      "CONDUCT_LOG": self.tmp / "conduct_log.jsonl",
                      "QUARANTINE": self.tmp / "quarantined_discoveries.json",
                      "REGISTER": self.tmp / "unsolved_register.json",
                      "CORRECTION_REPORT": self.tmp / "report.json",
                      # NEVER a passthrough of the live path: a getattr
                      # default here once wrote test counts into the REAL
                      # ~/.astra_persistent sidecar (2026-10-09).
                      "COUNT_SIDECAR": self.tmp / "lifetime_counts.json"}
        self.orig = {k: getattr(el, k) for k in self.paths}
        if verdict_rows is not None:
            _write_jsonl(self.paths["VERDICT_LOG"], verdict_rows)
        if ledger_rows is not None:
            _write_jsonl(self.paths["LEDGER"], ledger_rows)

    def __enter__(self):
        for k, v in self.paths.items():
            setattr(el, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.orig.items():
            setattr(el, k, v)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def test_lifetime_trials_sidecar():
    vrows = [{"claim": f"c{i}", "dataset": "d"} for i in range(5)]
    lrows = [{"kind": "trial", "disposition": "x"},
             {"kind": "anomaly", "disposition": "y"},
             {"kind": "note", "disposition": "z"}]
    with _IsolatedLedger(vrows, lrows) as iso:
        # 5 verdict-log trials + 2 countable ledger rows (trial+anomaly) = 7
        assert el.lifetime_trials() == 7
        assert iso.paths["COUNT_SIDECAR"].exists()
        # sidecar is now authoritative (monotonic): deleting the underlying
        # stores must NOT change the answer — no re-parse per append
        iso.paths["VERDICT_LOG"].unlink()
        iso.paths["LEDGER"].unlink()
        assert el.lifetime_trials() == 7, "sidecar not honoured after backfill"
        # append() grows the count exactly per current semantics
        el.append("trial", "x", claim="c9")
        assert el.lifetime_trials() == 8
        el.append("note", "y")
        assert el.lifetime_trials() == 8, "notes are not trials"
    RESULTS.append(("point12 lifetime sidecar", True))


def test_verdict_log_append_bumps_counts():
    with _IsolatedLedger([], []) as iso:
        orig_vl = rcs.VERDICT_LOG
        rcs.VERDICT_LOG = iso.tmp / "claim_verdicts.jsonl"
        try:
            before = el.lifetime_trials()
            rcs._append_verdict_log({
                "claim": "t", "dataset": "d", "program_hash": "h",
                "both_pass": False, "gate1": {"pass": True, "metrics": {}},
                "gate2": {"status": "known"},
            }, label="seed")
            assert el.lifetime_trials() == before + 1, \
                "verdict-log append did not bump the lifetime count"
            assert rcs.VERDICT_LOG.exists()
        finally:
            rcs.VERDICT_LOG = orig_vl
    RESULTS.append(("point12 verdict append bumps counts", True))


def test_io_tail_helper():
    from evolved_analysis import _io_tail
    with _Tmp() as tmp:
        p = tmp / "x.jsonl"
        rows = [{"i": i} for i in range(100)]
        _write_jsonl(p, rows)
        got = _io_tail.tail_jsonl(p, max_bytes=300)
        assert got and got[-1]["i"] == 99 and got[0]["i"] > 0, \
            "tail must be the LAST rows in file order"
        assert got == rows[-len(got):]
        # malformed lines skipped, short file returns everything
        with p.open("a") as f:
            f.write("not json\n")
        got2 = _io_tail.tail_jsonl(p, max_bytes=10**9)
        assert len(got2) == 100
        # last_matching: last n rows for a predicate, file order
        m, hit_cap = _io_tail.last_matching(
            p, lambda r: r["i"] % 2 == 0, n=3, max_bytes=10**9)
        assert [r["i"] for r in m] == [94, 96, 98] and not hit_cap
        m2, hit_cap2 = _io_tail.last_matching(
            p, lambda r: False, n=3, max_bytes=500)
        assert m2 == [] and hit_cap2
    RESULTS.append(("point12 _io_tail helper", True))


def test_dataset_gate1_history_tail_equivalence():
    rows = []
    for i in range(400):
        ds = "d1" if i % 3 == 0 else "d2"
        rows.append({"dataset": ds, "gate1": {"pass": i % 7 == 0},
                     "ts": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"})
    with _Tmp() as tmp:
        p = tmp / "claim_verdicts.jsonl"
        _write_jsonl(p, rows)
        orig = rcs.VERDICT_LOG
        rcs.VERDICT_LOG = p
        try:
            got = rcs._dataset_gate1_history("d1", window=20)
            # ground truth: filter + last 20
            d1 = [r for r in rows if r["dataset"] == "d1"][-20:]
            assert got["n_trials"] == 20
            assert got["n_gate1_pass"] == sum(
                1 for r in d1 if r["gate1"]["pass"] is True)
        finally:
            rcs.VERDICT_LOG = orig
    RESULTS.append(("point12 gate1 history tail equivalence", True))


def test_predictions_tail_read_and_rotation():
    rows = []
    for ep in range(30):
        rows.append({"ts": f"2026-01-0{ep % 9 + 1}T00:00:00", "label": "seed",
                     "dataset": "sdss_qso", "claim": f"s{ep}",
                     "gate1": {"pass": True}, "both_pass": False})
        for s in range(3):
            rows.append({"ts": f"2026-01-0{ep % 9 + 1}T00:0{s}:00",
                         "label": f"step{s}", "dataset": "sdss_qso",
                         "claim": f"c{ep}-{s}",
                         "gate1": {"pass": s == 0}, "both_pass": False})
    with _Tmp() as tmp:
        vl = tmp / "claim_verdicts.jsonl"
        _write_jsonl(vl, rows)
        orig_v, orig_p, orig_s = (pred.VERDICT_LOG, pred.PREDICTIONS,
                                  pred.SURPRISE_LEDGER)
        pred.VERDICT_LOG = vl
        pred.PREDICTIONS = tmp / "predictions.jsonl"
        pred.SURPRISE_LEDGER = tmp / "surprise_ledger.jsonl"
        try:
            full = pred.load_verdicts()
            assert len(full) == 120
            capped = pred.load_verdicts(max_bytes=200)
            assert 0 < len(capped) < 120, "tail cap not applied"
            assert capped == full[-len(capped):]
            # forecast unchanged in shape on the tail
            stats = pred.historical_stats("sdss_qso", pred.load_verdicts(
                max_bytes=200))
            assert stats is not None and stats["n_episodes"] >= 1
            # rotation on append
            pred.PREDICTIONS_CAP_BYTES = 10
            pred.SURPRISE_CAP_BYTES = 10
            p = pred.Prediction("ds", "2026-01-01T00:00:00", 0.5, 0.1, 0.4, 0.9,
                                "ku", 3)
            pred.write_prediction(p)
            assert (tmp / "predictions.jsonl.1").exists()
            pred.append_surprise(p, {"gate1_pass_rate": 0.5,
                                     "novel_emits": 0}, 0.2)
            assert (tmp / "surprise_ledger.jsonl.1").exists()
        finally:
            pred.VERDICT_LOG, pred.PREDICTIONS, pred.SURPRISE_LEDGER = \
                orig_v, orig_p, orig_s
    RESULTS.append(("point12 predictions tail + rotation", True))


def test_explored_themes_tail():
    rows = [{"dataset": "d1" if i % 2 else "d2", "claim": f"claim {i}",
             "ts": f"2026-01-01T00:00:{i:02d}",
             "gate2": {"status": "known"}, "gate1": {"pass": True},
             "both_pass": False}
            for i in range(60)]
    with _Tmp() as tmp:
        # data_lake reads LAKE_DIR.parent / claim_verdicts.jsonl
        vl = tmp / "claim_verdicts.jsonl"
        _write_jsonl(vl, rows)
        orig = dl.LAKE_DIR
        dl.LAKE_DIR = tmp / "data_lake"
        try:
            themes = dl.explored_themes("d1", n=5)
            expect = [r for r in rows if r["dataset"] == "d1"]
            expect.sort(key=lambda r: r["ts"], reverse=True)
            assert len(themes) == 5
            assert themes[0][0].startswith(expect[0]["claim"][:20]), \
                "explored_themes must return MOST-RECENT-first themes"
        finally:
            dl.LAKE_DIR = orig
    RESULTS.append(("point12 explored_themes tail", True))


def test_consumer_mtime_short_circuit():
    class _Sys:
        genuine_discoveries = []

    with _Tmp() as tmp:
        store = tmp / "evolved_discoveries.json"
        store.write_text(json.dumps([
            {"title": "t1", "abstract": "a", "discovery_type": "x",
             "timestamp": "2026", "verification": {"program_hash": "h1"},
             "source": "evolved_analysis"},
        ]))
        orig = edc.EVOLVED_STORE
        edc.EVOLVED_STORE = lambda: store
        try:
            sys1 = _Sys()
            assert edc.consume_evolved_discoveries(sys1) == 1
            sig = edc._store_signature(store)
            assert sig is not None and edc._LAST_SEEN == sig
            # unchanged store: no parse (signature short-circuit)
            assert edc._store_unchanged(store) is True
            assert edc.consume_evolved_discoveries(_Sys()) == 0
            # changed store: parse again
            store.write_text(json.dumps([
                {"title": "t1", "abstract": "a", "discovery_type": "x",
                 "timestamp": "2026", "verification": {"program_hash": "h1"},
                 "source": "evolved_analysis"},
                {"title": "t2", "abstract": "b", "discovery_type": "x",
                 "timestamp": "2026", "verification": {"program_hash": "h2"},
                 "source": "evolved_analysis"},
            ]))
            assert edc._store_unchanged(store) is False
            sys2 = _Sys()
            assert edc.consume_evolved_discoveries(sys2) == 1
        finally:
            edc.EVOLVED_STORE = orig
    RESULTS.append(("point12 consumer mtime short-circuit", True))


def test_precheck_log_rotation():
    with _Tmp() as tmp:
        p = tmp / "novelty_precheck.jsonl"
        orig = npc.PRECHECK_PATH
        cap_orig = getattr(npc, "PRECHECK_CAP_BYTES", None)
        npc.PRECHECK_PATH = p
        npc.PRECHECK_CAP_BYTES = 10
        try:
            npc.log_precheck({"top_similarity": 0.5}, "known", path=p)
            assert (tmp / "novelty_precheck.jsonl.1").exists()
        finally:
            npc.PRECHECK_PATH = orig
            if cap_orig is not None:
                npc.PRECHECK_CAP_BYTES = cap_orig
    RESULTS.append(("point12 precheck log rotation", True))


# --------------------------------------------------------------------------- #
# runner                                                                       #
# --------------------------------------------------------------------------- #
def _main():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
