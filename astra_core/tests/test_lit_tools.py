"""Tests for lit_tools.py — v14.19 (2026-09-19): papers-as-tested-tools.

Pins the honesty contract of the literature-tool registry:
  * registration is EARNED: a real tool directory whose self-test passes in a
    REAL subprocess becomes ``verified``; incomplete metadata or a failing
    self-test is recorded as ``rejected`` (kept, with reasons) and is never
    runnable;
  * a tool whose directory disappears is marked ``broken``, never deleted;
  * ``run`` executes ONLY verified tools, under the JSON job contract, and
    reports failures as themselves (never raises);
  * ``request`` — the on-demand agentification queue — dedups and ledgers a
    note; ``missing_tool_note`` files exactly one pending request per tool;
  * the falsification arm stamping: a prediction naming a verified tool is
    ``executed_via="lit-tool:<id>"``; naming an unregistered tool files the
    request and stays honestly in-house.

Tool fixtures are REAL: genuine trivial directories with working selftest.py /
run.py, executed as real subprocesses. No persistent store outside a tempdir
is touched (registry + ledger redirected).

Run: python3 astra_core/tests/test_lit_tools.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from astra_core.scientific_discovery.evolved_analysis import lit_tools as lt  # noqa: E402
from astra_core.scientific_discovery.evolved_analysis import evidence_ledger as el  # noqa: E402


class _Isolated:
    """Redirect the registry + evidence ledger to a tempdir."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lit_tools_test_"))
        self.lt_paths = {"REGISTRY": self.tmp / "lit_tools.json",
                         "EVOLVED_PROGRAMS": self.tmp}
        self.el_paths = {"EVOLVED_PROGRAMS": self.tmp,
                         "LEDGER": self.tmp / "evidence_ledger.jsonl",
                         "VERDICT_LOG": self.tmp / "claim_verdicts.jsonl",
                         "CONDUCT_LOG": self.tmp / "conduct_log.jsonl",
                         "QUARANTINE": self.tmp / "quarantined_discoveries.json",
                         "REGISTER": self.tmp / "unsolved_register.json",
                         "CORRECTION_REPORT": self.tmp / "lifetime_correction_report.json"}

    def __enter__(self):
        self.orig = {}
        for mod, paths in ((lt, self.lt_paths), (el, self.el_paths)):
            for k, v in paths.items():
                self.orig[(id(mod), k)] = getattr(mod, k)
                setattr(mod, k, v)
        return self

    def __exit__(self, *exc):
        for mod, paths in ((lt, self.lt_paths), (el, self.el_paths)):
            for k in paths:
                setattr(mod, k, self.orig[(id(mod), k)])
        return False


_GOOD_META = {
    "tool_id": "kelly2007-linmix",
    "paper_title": "Some Assaying Method (2007)",
    "paper_year": 2007,
    "code_url": "https://github.com/example/linmix",
    "arxiv_or_doi": "arXiv:0707.0000",
    "method": "linear regression with measurement errors",
}

_SELFTEST = ("import json\n"
             "x = 2 + 2\n"
             "assert x == 4, 'known answer wrong'\n"
             "print(json.dumps({'ok': True, 'detail': '2+2 recovered'}))\n")

_RUN = ("import json, sys\n"
        "job = json.load(open(sys.argv[1]))\n"
        "job['doubled'] = job['x'] * 2\n"
        "print(json.dumps(job))\n")


def _make_tool(tmp: Path, name: str, *, meta=_GOOD_META, selftest=_SELFTEST,
               run=_RUN, tool_id=None):
    d = tmp / name
    d.mkdir()
    m = dict(meta)
    if tool_id:
        m["tool_id"] = tool_id
    (d / "TOOL.json").write_text(json.dumps(m))
    (d / "selftest.py").write_text(selftest)
    (d / "run.py").write_text(run)
    return d


# --------------------------------------------------------------------------- #
# registration: earned, or honestly rejected                                    #
# --------------------------------------------------------------------------- #
def test_register_good_tool_verifies_with_real_selftest():
    with _Isolated() as iso:
        d = _make_tool(iso.tmp, "good")
        rec = lt.register(str(d))
        assert rec["status"] == "verified", rec["history"]
        assert rec["tool_id"] == "kelly2007-linmix"
        assert rec["paper"]["paper_title"] == _GOOD_META["paper_title"]
        st = lt.status()
        assert st["n_tools"] == 1 and st["by_status"].get("verified") == 1
        # the paper metadata travels with the provenance stamp
        prov = lt.provenance("kelly2007-linmix")
        assert prov and prov["executed_via"] == "lit-tool:kelly2007-linmix"
        assert prov["paper"]["arxiv_or_doi"] == "arXiv:0707.0000"


def test_register_incomplete_metadata_is_rejected_and_kept():
    with _Isolated() as iso:
        bad = dict(_GOOD_META)
        del bad["code_url"]           # a tool without its cited code is noise
        d = _make_tool(iso.tmp, "nometa", meta=bad)
        rec = lt.register(str(d))
        assert rec["status"] == "rejected"
        assert "code_url" in rec["history"][-1]["detail"]
        # rejected attempts are VISIBLE, not silently dropped
        assert lt.status()["n_tools"] == 1
        assert lt.provenance(rec["tool_id"] or "x") is None


def test_register_failing_selftest_is_rejected():
    with _Isolated() as iso:
        d = _make_tool(iso.tmp, "badtest", selftest=_SELFTEST.replace("== 4", "== 5"))
        rec = lt.register(str(d))
        assert rec["status"] == "rejected", rec["history"]
        # and the tool cannot be run (unverified literature code never executes)
        rr = lt.run(rec["tool_id"] or "", {"x": 1})
        assert rr["ok"] is False and "not 'verified'" in rr["error"]


def test_register_missing_directory_is_rejected():
    with _Isolated() as iso:
        rec = lt.register(str(iso.tmp / "does_not_exist"))
        assert rec["status"] == "rejected"
        assert "not a directory" in rec["history"][-1]["detail"]


# --------------------------------------------------------------------------- #
# lifecycle: verify / broken                                                   #
# --------------------------------------------------------------------------- #
def test_verify_flips_to_failed_when_selftest_breaks():
    with _Isolated() as iso:
        d = _make_tool(iso.tmp, "flaky")
        assert lt.register(str(d))["status"] == "verified"
        (d / "selftest.py").write_text("raise SystemExit(3)\n")
        res = lt.verify("kelly2007-linmix")
        assert res["status"] == "failed"
        assert lt.get("kelly2007-linmix")["status"] == "failed"
        # a failed tool is refused by run()
        rr = lt.run("kelly2007-linmix", {"x": 1})
        assert rr["ok"] is False


def test_disappearing_directory_is_marked_broken_not_deleted():
    with _Isolated() as iso:
        d = _make_tool(iso.tmp, "gone")
        assert lt.register(str(d))["status"] == "verified"
        import shutil
        shutil.rmtree(d)
        tool = lt.get("kelly2007-linmix")     # _load self-validates
        assert tool["status"] == "broken"
        assert any("disappeared" in h["detail"] for h in tool["history"])
        # broken tools never run
        assert lt.run("kelly2007-linmix", {"x": 1})["ok"] is False


# --------------------------------------------------------------------------- #
# run contract                                                                 #
# --------------------------------------------------------------------------- #
def test_run_executes_verified_tool_under_json_contract():
    with _Isolated() as iso:
        d = _make_tool(iso.tmp, "runner")
        lt.register(str(d))
        rr = lt.run("kelly2007-linmix", {"x": 21})
        assert rr["ok"] is True
        assert rr["result"]["doubled"] == 42        # real subprocess, real math
        # the job file is cleaned up (no droppings in the tool dir)
        assert not (d / ".astra_job.json").exists()


def test_run_unknown_tool_reports_itself():
    with _Isolated():
        rr = lt.run("no-such-tool", {"x": 1})
        assert rr["ok"] is False and "unknown tool" in rr["error"]


# --------------------------------------------------------------------------- #
# the on-demand queue                                                           #
# --------------------------------------------------------------------------- #
def test_request_dedups_and_ledgers():
    with _Isolated() as iso:
        r1 = lt.request("Linear regression with measurement errors",
                        "https://github.com/example/linmix",
                        why="falsification arm needs Kelly 2007 slope",
                        claim="C1", dataset="sdss_qso")
        r2 = lt.request("Linear regression with measurement errors",
                        "https://github.com/example/linmix",
                        why="again")
        assert r2.get("duplicate") is True
        st = lt.status()
        assert st["n_pending_requests"] == 1
        # the ledger note exists exactly once
        notes = [e for e in el.read_ledger()
                 if e.get("kind") == "note"
                 and e.get("disposition") == "lit-tool-requested"]
        assert len(notes) == 1
        assert "Kelly 2007" in notes[0]["detail"]


def test_missing_tool_note_files_once_per_tool():
    with _Isolated():
        note1 = lt.missing_tool_note("kelly2007-linmix", "prediction P1 names it")
        note2 = lt.missing_tool_note("kelly2007-linmix", "prediction P2 names it")
        assert note1 == "lit-tool-requested"
        assert note2 == "lit-tool-request-already-pending"
        reqs = [r for r in json.loads(lt.REGISTRY.read_text())["requests"]
                if r.get("tool_id") == "kelly2007-linmix"]
        assert len(reqs) == 1


# --------------------------------------------------------------------------- #
# falsification-arm stamping                                                    #
# --------------------------------------------------------------------------- #
def _prediction(lit_tool=None):
    from astra_core.scientific_discovery.falsification import FalsifiablePrediction
    return FalsifiablePrediction(
        id="test-pred", model="TestModel", model_citation="Author 2007, ApJ",
        quantity="slope", units="dimensionless", system_class="test",
        formula_doc="y = a*x (a=2)",
        predict=lambda inp: (2.0 * inp["x"], 0.01),
        fetch=lambda sid: {"x": 1.5},
        observe=lambda sid: (3.0, 0.01),
        observe_citation="Obs 2007", lit_tool=lit_tool)


def test_engine_stamps_verified_lit_tool():
    from astra_core.scientific_discovery.falsification import evaluate
    with _Isolated() as iso:
        d = _make_tool(iso.tmp, "good")
        assert lt.register(str(d))["status"] == "verified"
        res = evaluate(_prediction(lit_tool="kelly2007-linmix"), "sys1")
        assert res.executed_via == "lit-tool:kelly2007-linmix"
        assert res.is_anomaly is False      # 2*1.5 == 3.0 -> model confirmed


def test_engine_files_request_for_named_but_unregistered_tool():
    from astra_core.scientific_discovery.falsification import evaluate
    with _Isolated():
        res = evaluate(_prediction(lit_tool="kelly2007-linmix"), "sys1")
        # honest: stays in-house, the absence is queued, nothing fabricated
        assert res.executed_via == "in-house-code;lit-tool-requested"
        assert lt.status()["n_pending_requests"] == 1


def test_engine_default_stays_in_house():
    from astra_core.scientific_discovery.falsification import evaluate
    with _Isolated():
        res = evaluate(_prediction(), "sys1")
        assert res.executed_via == "in-house-code"
        assert lt.status()["n_pending_requests"] == 0


def test_registry_failure_degrades_to_in_house():
    """If the registry infrastructure raises, evaluation must not break —
    the prediction stays honestly in-house."""
    from astra_core.scientific_discovery.falsification import engine as fe
    with _Isolated():
        orig = lt.provenance

        def boom(tool_id):
            raise RuntimeError("registry exploded")

        lt.provenance = boom
        try:
            res = fe.evaluate(_prediction(lit_tool="kelly2007-linmix"), "sys1")
        finally:
            lt.provenance = orig
        assert res.executed_via == "in-house-code"


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as ex:
            failed += 1
            print(f"FAIL {fn.__name__}: {ex}")
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(ex).__name__}: {ex}")
        else:
            print(f"PASS {fn.__name__}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
