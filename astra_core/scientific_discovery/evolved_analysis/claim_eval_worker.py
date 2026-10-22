"""claim_eval_worker.py — isolated subprocess that runs ONE claim candidate's
real-data test (Gate 1 of the two-gate EVALUATE).

Invoked as:
    python -m evolved_analysis.claim_eval_worker <source_file> [seed] [data_source]

An optional third arg ``data_source`` selects a data-lake dataset
(data_lake.py, Sub-project C); omit it (or pass 'legacy') to use the default
SDSS photo-z sample via real_data.py. The worker only ever READS a cached CSV —
it never fetches (no network from the sandbox); caches are populated by
data_lake.fetch_and_cache() outside the sandbox.

It loads the candidate (a module-level CLAIM + a ``run_claim(df_train, df_eval)``
function), runs it on REAL data, and prints ONE line of JSON to stdout:
    {"effect": ..., "pvalue": ..., "effect_type": ..., "summary": ..., "claim": ...}

Defence-in-depth (identical to eval_worker): ``resource`` caps + the AST safety
gate in safety.py + (when launched via sandbox-exec) the no-network profile. The
claim string is echoed back so the orchestrator can run Gate 2 (novelty) on it.
"""
from __future__ import annotations

import json
import os
import resource
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
from .real_data import load_split  # noqa: E402
from .claim_task import parse_claim, ENTRY_POINT  # noqa: E402

# Fresh-attacker permutation mode (Phase 2, Eureka plan 2026-08-30): when
# ASTRA_ATTACK_PERMUTE is set to an integer, every column of every split is
# independently row-shuffled with that seed BEFORE the claim runs. This breaks
# all cross-column associations while preserving each column's marginal — the
# generic empty-context null. An effect that survives this measures a
# marginal, not an association, and is flagged rather than trusted.
_PERMUTE_ENV = "ASTRA_ATTACK_PERMUTE"

# Artifact-harness signal mode (v14.19, 2026-09-19): when ASTRA_HARNESS_SIGNAL
# is set to an integer, the splits are REPLACED by rank-coupled synthetic
# frames carrying a planted near-deterministic cross-column association (the
# exact dual of the permutation null: marginals still preserved exactly, but
# association PLANTED instead of destroyed). A claim statistic that cannot
# detect this known answer is unresponsive — flagged by artifact_verification,
# never silently trusted. Used ONLY by the promotion-time artifact harness;
# never produces a discovery, emission, or store write of its own.
_SIGNAL_ENV = "ASTRA_HARNESS_SIGNAL"

# Resource caps (defence-in-depth). RLIMIT_AS is skipped on macOS (premature
# kill). Applied in main(), NOT at import: setrlimit at module level is an
# import side effect that irreversibly caps any importing process (hard
# RLIMIT_NPROC can only be lowered) — the worker's limits belong to the worker
# process alone, and main() runs them before any claim code is exec'd, so the
# protection is identical.
def _apply_resource_limits():
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except Exception:
        pass


def _permute_splits(splits: dict, permute_seed: int) -> None:
    """Independently row-shuffle every column of every split in place.

    The attacker's generic null: destroys ALL cross-column associations,
    preserves each column's marginal exactly. A claim whose headline effect
    survives this is measuring a single-column property, not a relation.
    """
    rng = np.random.default_rng(permute_seed)
    for name, df in list(splits.items()):
        shuffled = df.copy()
        for col in df.columns:
            shuffled[col] = df[col].to_numpy()[rng.permutation(len(df))]
        splits[name] = shuffled


def _signal_splits(splits: dict, signal_seed: int, noise: float = 0.25) -> None:
    """Plant a near-deterministic association into every split, in place.

    The dual of ``_permute_splits``: each NUMERIC column's values are re-ordered
    so their ranks follow one shared latent factor ``z + noise`` (pairwise rank
    correlation ≈ 1/(1+noise²) ≈ 0.94 at the default noise), so every numeric
    column pair carries a strong monotone association — while each column's
    marginal is preserved EXACTLY (the re-ordered values are the real column's
    own sorted values; a rank-coupling copula). Non-numeric columns are
    left untouched (no planted signal, no schema breakage for groupby-style
    programs). This is the known-answer frame for the artifact harness: a
    well-specified association statistic MUST fire on it, just as it must stay
    null on the permutation frame. Machinery calibration only — never a
    discovery, never emitted.
    """
    rng = np.random.default_rng(signal_seed)
    for name, df in list(splits.items()):
        n = len(df)
        z = rng.normal(size=n)
        planted = df.copy()
        for col in df.columns:
            if not np.issubdtype(planted[col].dtype, np.number):
                continue
            ranks = np.argsort(np.argsort(z + rng.normal(scale=noise, size=n)))
            planted[col] = np.sort(df[col].to_numpy())[ranks]
        splits[name] = planted


def main():
    _apply_resource_limits()   # before ANY claim code is loaded or exec'd
    src_path = sys.argv[1]
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    source = sys.argv[3] if len(sys.argv) > 3 else "legacy"
    try:
        if source and source != "legacy":
            # Sub-project C: read a cached data-lake dataset (file read only,
            # never fetches — the sandbox has no network).
            from .data_lake import load_split as _lake_split
            splits = _lake_split(source, seed=seed)
        else:
            splits = load_split(seed=seed)

        if os.environ.get(_PERMUTE_ENV):
            _permute_splits(splits, int(os.environ["ASTRA_ATTACK_PERMUTE"]))
        if os.environ.get(_SIGNAL_ENV):
            # artifact-harness known-answer mode (v14.19); mutually exclusive
            # with the permutation null in practice
            _signal_splits(splits, int(os.environ["ASTRA_HARNESS_SIGNAL"]))
        src = Path(src_path).read_text()

        # AST safety gate BEFORE exec (catches os/subprocess/open/eval/...).
        from .safety import check_source
        ok, reason = check_source(src, entry_point=ENTRY_POINT)
        if not ok:
            print(json.dumps({"effect": 0.0, "pvalue": 1.0, "error": f"blocked:{reason}"}))
            return

        ns: dict = {}
        exec(compile(src, src_path, "exec"), ns)
        fn = ns.get(ENTRY_POINT)
        if not callable(fn):
            raise RuntimeError(f"source does not define {ENTRY_POINT}(df_train, df_eval)")

        def _call(split_first, split_second, label):
            """Run the claim fn on one split pair; return its dict or an error dict."""
            try:
                r = fn(splits[split_first], splits[split_second])
            except Exception as e:  # claim code fault on this split -> fail closed
                return {"effect": 0.0, "pvalue": 1.0,
                        "error": f"{label}:{type(e).__name__}:{str(e)[:120]}"}
            if not isinstance(r, dict):
                return {"effect": 0.0, "pvalue": 1.0,
                        "error": f"{label}:non-dict return"}
            return r

        # Fix 2 — the HEADLINE statistic is the held-out one. The search used to
        # report run_claim on df_train only (in-sample); df_eval/df_test were
        # passed but ignored. We now evaluate twice:
        #   * insample = fn(train, eval)  -> what the search effectively saw
        #   * holdout  = fn(test,  eval)  -> genuinely unseen galaxies (test)
        # Most claims compute on their FIRST arg (the seed does `df = df_train`),
        # so fn(test, ...) computes the statistic on the untouched test split.
        insample = _call("train", "eval", "insample")
        holdout = _call("test", "eval", "holdout")

        result = dict(holdout)  # hold-out is primary: effect / pvalue / summary
        if "error" not in result:
            result["effect_insample"] = float(insample.get("effect", 0.0))
            result["pvalue_insample"] = float(insample.get("pvalue", 1.0))
            if insample.get("effect_type"):
                result["effect_type_insample"] = insample.get("effect_type")
        # echo the claim text back for Gate 2
        result.setdefault("claim", parse_claim(src) or "")
        # sanity: numeric effect/pvalue
        for k in ("effect", "pvalue"):
            if k in result:
                result[k] = float(result[k])
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({
            "effect": 0.0, "pvalue": 1.0,
            "error": f"{type(e).__name__}: {str(e)[:160]}",
            "trace": traceback.format_exc(limit=2),
        }))


if __name__ == "__main__":
    main()
