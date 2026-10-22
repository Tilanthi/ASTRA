"""Tests for artifact_verification.py — v14.19 (2026-09-19): artifact-level
verification at the promotion chokepoint.

Pins:
  * ``_signal_splits`` (the known-answer frame generator in
    claim_eval_worker): every numeric column's marginal preserved EXACTLY, a
    strong monotone association PLANTED between numeric columns (the dual of
    the permutation null, which destroys it), non-numeric columns untouched;
  * ``signal_check`` statuses (detected / not-detected / no-pvalue / error)
    with the sandbox run stubbed — and ONE real end-to-end run through the
    actual claim_eval_worker subprocess on the cached real SDSS sample
    (skipped honestly if the cache is absent);
  * ``env_manifest`` pins third-party versions for exactly the imported
    modules, stdlib excluded, deterministically;
  * rule-based harness labels; ``verify_artifact`` never raises / never
    blocks; the ledger note fires ONLY on a definitive not-detected;
  * the _emit chokepoint integration: the report travels with the verdict
    AND the emitted store record, emission still succeeds when the harness
    fails or errors.

No persistent store outside a tempdir is touched.

Run: python3 astra_core/tests/test_artifact_verification.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from astra_core.scientific_discovery.evolved_analysis import artifact_verification as av  # noqa: E402
from astra_core.scientific_discovery.evolved_analysis import evidence_ledger as el       # noqa: E402
from astra_core.scientific_discovery.evolved_analysis import run_claim_search as rcs     # noqa: E402
from astra_core.scientific_discovery.evolved_analysis import claim_eval_worker as cew    # noqa: E402


class _Isolated:
    """Redirect the evidence ledger to a tempdir."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact_verification_test_"))
        self.paths = {"EVOLVED_PROGRAMS": self.tmp,
                      "LEDGER": self.tmp / "evidence_ledger.jsonl",
                      "VERDICT_LOG": self.tmp / "claim_verdicts.jsonl",
                      "CONDUCT_LOG": self.tmp / "conduct_log.jsonl",
                      "QUARANTINE": self.tmp / "quarantined_discoveries.json",
                      "REGISTER": self.tmp / "unsolved_register.json",
                      "CORRECTION_REPORT": self.tmp / "lifetime_correction_report.json"}

    def __enter__(self):
        self.orig = {}
        for k, v in self.paths.items():
            self.orig[k] = getattr(el, k)
            setattr(el, k, v)
        return self

    def __exit__(self, *exc):
        for k in self.paths:
            setattr(el, k, self.orig[k])
        return False


class _Monkey:
    """Minimal monkeypatch (plain-runner compatible)."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo = []


# --------------------------------------------------------------------------- #
# the known-answer frame generator                                              #
# --------------------------------------------------------------------------- #
def test_signal_splits_preserves_marginals_plants_association():
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(7)
    n = 400
    # independent numeric columns (no association by construction) + a string col
    df_real = pd.DataFrame({
        "u": rng.normal(18, 1, n),
        "g": rng.normal(19, 1, n),
        "cls": np.random.default_rng(1).choice(["G", "Q", "S"], n),
    })
    splits = {"train": df_real.copy(), "test": df_real.copy()}
    # before: numeric columns essentially unassociated
    before = np.corrcoef(np.argsort(np.argsort(df_real["u"])),
                         np.argsort(np.argsort(df_real["g"])))[0, 1]
    assert abs(before) < 0.2

    cew._signal_splits(splits, signal_seed=42)

    for name, planted in splits.items():
        # marginal preserved EXACTLY: same sorted multiset of real values
        for col in ("u", "g"):
            assert np.array_equal(np.sort(planted[col].to_numpy()),
                                  np.sort(df_real[col].to_numpy()))
        # string column untouched (no schema breakage, no planted signal)
        assert (planted["cls"] == df_real["cls"]).all()
        # strong monotone association planted
        ru = np.argsort(np.argsort(planted["u"].to_numpy()))
        rg = np.argsort(np.argsort(planted["g"].to_numpy()))
        after = np.corrcoef(ru, rg)[0, 1]
        assert after > 0.85, f"planted association too weak: {after}"


def test_signal_splits_is_deterministic_per_seed():
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"a": rng.normal(size=200), "b": rng.normal(size=200)})
    s1 = {"x": df.copy()}
    s2 = {"x": df.copy()}
    cew._signal_splits(s1, 123)
    cew._signal_splits(s2, 123)
    assert np.array_equal(s1["x"]["a"].to_numpy(), s2["x"]["a"].to_numpy())
    cew._signal_splits(s2, 124)
    assert not np.array_equal(s1["x"]["a"].to_numpy(), s2["x"]["a"].to_numpy())


# --------------------------------------------------------------------------- #
# signal_check statuses (sandbox stubbed)                                       #
# --------------------------------------------------------------------------- #
def _stub_gate1(monkeypatch, result):
    calls = []

    def fake(src, seed=42, source="legacy", timeout=90.0, permute_seed=None,
             signal_seed=None):
        calls.append({"seed": seed, "signal_seed": signal_seed, "source": source})
        return dict(result)
    monkeypatch.setattr(rcs, "gate1_run", fake)
    return calls


def test_signal_check_detected(monkeypatch):
    calls = _stub_gate1(monkeypatch, {"effect": 0.9, "pvalue": 1e-12})
    res = av.signal_check("CLAIM='x'\ndef run_claim(a,b):\n    return {}\n",
                          "sdss_qso", pmax=4e-8)
    assert res["status"] == "detected"
    assert res["pvalue"] == 1e-12 and res["pmax"] == 4e-8
    # the run used the signal frame and the module's pinned seed
    assert calls[0]["signal_seed"] == av.SIGNAL_SEED
    assert calls[0]["source"] == "sdss_qso"


def test_signal_check_not_detected_is_definitive(monkeypatch):
    _stub_gate1(monkeypatch, {"effect": 0.001, "pvalue": 0.6})
    res = av.signal_check("src", "legacy", pmax=4e-8)
    assert res["status"] == "not-detected"


def test_signal_check_no_pvalue_and_error(monkeypatch):
    _stub_gate1(monkeypatch, {"effect": 0.3})
    assert av.signal_check("src", "legacy", pmax=1e-3)["status"] == "no-pvalue"
    _stub_gate1(monkeypatch, {"effect": 0.0, "error": "timeout"})
    assert av.signal_check("src", "legacy", pmax=1e-3)["status"] == "error"


def test_labels_follow_rules():
    assert av.harness_label("detected") == "harness-passed"
    assert av.harness_label("not-detected") == "harness-marked"
    assert av.harness_label("error") == "harness-not-runnable"
    assert av.harness_label("no-pvalue") == "harness-inconclusive"
    assert av.harness_label("anything-else") == "harness-inconclusive"


# --------------------------------------------------------------------------- #
# env manifest                                                                  #
# --------------------------------------------------------------------------- #
def test_env_manifest_pins_imported_third_party_only():
    m1 = av.env_manifest("import json\nimport numpy\nfrom pandas import DataFrame\n")
    m2 = av.env_manifest("import json\nimport numpy\nfrom pandas import DataFrame\n")
    assert m1 == m2                                    # deterministic
    assert m1["python"] and m1["platform"]
    assert "numpy" in m1["package_pins"] and "pandas" in m1["package_pins"]
    assert "json" not in m1["package_pins"]            # stdlib: no pin
    import importlib.metadata as md
    assert m1["package_pins"]["numpy"] == md.version("numpy")


# --------------------------------------------------------------------------- #
# verify_artifact: never blocks, ledger note only on definitive flag            #
# --------------------------------------------------------------------------- #
def _verdict(pmax=4e-8):
    return {"claim": "A claim.", "dataset": "sdss_qso", "program_hash": "h0",
            "source": "import numpy\nCLAIM='x'\ndef run_claim(a,b):\n    return {}\n",
            "gate1": {"bonferroni_pmax": pmax,
                      "metrics": {"effect": 0.4, "pvalue": 1e-9}}}


def test_verify_artifact_pass_and_flag(monkeypatch):
    with _Isolated():
        _stub_gate1(monkeypatch, {"effect": 0.9, "pvalue": 1e-12})
        rep = av.verify_artifact(_verdict())
        assert rep["harness_label"] == "harness-passed"
        assert rep["signal_check"]["status"] == "detected"
        assert "numpy" in rep["env"]["package_pins"]
        assert not any(e.get("disposition") == "artifact-harness-flagged"
                       for e in el.read_ledger())      # no note on a pass

        _stub_gate1(monkeypatch, {"effect": 0.001, "pvalue": 0.5})
        rep = av.verify_artifact(_verdict())
        assert rep["harness_label"] == "harness-marked"
        notes = [e for e in el.read_ledger()
                 if e.get("disposition") == "artifact-harness-flagged"]
        assert len(notes) == 1                          # flagged exactly once
        assert notes[0]["dataset"] == "sdss_qso"


def test_verify_artifact_inconclusive_and_error_write_no_note(monkeypatch):
    with _Isolated():
        _stub_gate1(monkeypatch, {"effect": 0.3})            # no pvalue
        assert av.verify_artifact(_verdict())["harness_label"] == "harness-inconclusive"
        _stub_gate1(monkeypatch, {"effect": 0.0, "error": "timeout"})
        assert av.verify_artifact(_verdict())["harness_label"] == "harness-not-runnable"
        assert not any(e.get("disposition") == "artifact-harness-flagged"
                       for e in el.read_ledger())


def test_verify_artifact_never_raises():
    with _Isolated():
        rep = av.verify_artifact({"source": None})       # garbage verdict
        assert rep["harness_label"] == "harness-not-runnable"
        rep = av.verify_artifact({})
        assert "harness_label" in rep


# --------------------------------------------------------------------------- #
# end-to-end: the real worker, real cached data, real known answer              #
# --------------------------------------------------------------------------- #
# An honest statistician program: Spearman-style rank correlation between two
# real SDSS columns, reported with a p-value. On the signal frame the
# association is planted, so it MUST fire; this exercises the whole path
# (gate1_run signal_seed -> worker env -> _signal_splits -> program) for real.
_E2E_SRC = """
import math
import numpy as np
CLAIM = 'g-i colour rank-correlates with r-i colour in SDSS photometry'
def run_claim(df_train, df_eval):
    df = df_train
    ra = np.argsort(np.argsort(df['g'].to_numpy()))
    rb = np.argsort(np.argsort(df['i'].to_numpy()))
    n = len(df)
    rho = float(np.corrcoef(ra, rb)[0, 1])
    t = rho * np.sqrt(max(n - 2, 1) / max(1e-12, 1 - rho ** 2))
    p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / np.sqrt(2)))))
    return {'effect': rho, 'pvalue': p, 'effect_type': 'spearman_rho'}
"""


def test_end_to_end_known_answer_through_real_worker():
    from astra_core.scientific_discovery.evolved_analysis.real_data import CACHE_CSV
    if not CACHE_CSV.exists():
        print("SKIP e2e (no cached SDSS sample)")
        return
    # claim_eval_worker applies its resource limits in main() (not at import —
    # v14.19 fix), so importing it for the _signal_splits tests above leaves
    # this process free to spawn the real sandbox worker.
    res = av.signal_check(_E2E_SRC, "legacy", pmax=4e-8)
    assert res["status"] == "detected", res
    assert res["pvalue"] <= 4e-8


# --------------------------------------------------------------------------- #
# chokepoint integration (_emit)                                                #
# --------------------------------------------------------------------------- #
def _emit_verdict():
    return {
        "both_pass": True,
        "claim": "A novel claim about galaxies.",
        "source": _E2E_SRC,
        "program_hash": "deadbeef00",
        "dataset": "legacy",
        "gate1": {"metrics": {"effect": 0.47, "pvalue": 1e-20,
                              "effect_insample": 0.49, "pvalue_insample": 1e-22},
                  "bonferroni_pmax": 1.06e-5, "family_size": 94},
        "gate2": {"status": "novel"},
    }


def test_emit_attaches_artifact_verification_and_never_blocks(monkeypatch):
    import shutil
    from astra_core.scientific_discovery.evolved_analysis import fresh_attacker as fa
    # stub the attacker (covered elsewhere) so this test pins ONLY the
    # artifact-verification attachment; the harness runs stubbed too.
    monkeypatch.setattr(fa, "attack_claim",
                   lambda src, ds, **kw: {"disposition": "survived",
                                          "detail": "stubbed"})
    _stub_gate1(monkeypatch, {"effect": 0.9, "pvalue": 1e-12})
    fd, tmppath = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    Path(tmppath).write_text("[]")
    orig_store = rcs.EVOLVED_STORE
    rcs.EVOLVED_STORE = Path(tmppath)
    fd2, tmplog = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd2)
    Path(tmplog).write_text("")
    orig_vlog = rcs.VERDICT_LOG
    rcs.VERDICT_LOG = Path(tmplog)
    env_backup = {k: os.environ.pop(k) for k in
                  ("ASTRA_JUDGE2_TOKEN", "ASTRA_JUDGE2_MODEL", "ASTRA_JUDGE2_BASE_URL")
                  if k in os.environ}
    try:
        with _Isolated():
            v = _emit_verdict()
            assert rcs._emit(v) is True
            assert v["artifact_verification"]["harness_label"] == "harness-passed"
            rec = json.loads(Path(tmppath).read_text())[0]
            ver = rec["verification"]
            assert ver["artifact_verification"]["harness_label"] == "harness-passed"
            assert "numpy" in ver["artifact_verification"]["env"]["package_pins"]
            # verdict-log line carries the label for log-side queries
            rcs._append_verdict_log(v)
            line = json.loads(Path(tmplog).read_text().splitlines()[-1])
            assert line["artifact_verification"]["harness_label"] == "harness-passed"
            assert line["artifact_verification"]["signal"] == "detected"

            # harness ERROR must not block emission either
            v2 = _emit_verdict()
            _stub_gate1(monkeypatch, {"effect": 0.0, "error": "timeout"})
            Path(tmppath).write_text("[]")
            assert rcs._emit(v2) is True
            assert v2["artifact_verification"]["harness_label"] == "harness-not-runnable"
    finally:
        rcs.EVOLVED_STORE = orig_store
        rcs.VERDICT_LOG = orig_vlog
        os.environ.update(env_backup)
        os.unlink(tmppath)
        os.unlink(tmplog)


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        monkey = _Monkey()
        kwargs = {}
        if "monkey" in inspect.signature(fn).parameters:
            kwargs["monkey"] = monkey
        try:
            fn(**kwargs)
        except AssertionError as ex:
            failed += 1
            print(f"FAIL {fn.__name__}: {ex}")
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(ex).__name__}: {ex}")
        else:
            print(f"PASS {fn.__name__}")
        finally:
            monkeypatch.undo()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
