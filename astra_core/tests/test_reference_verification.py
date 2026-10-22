"""Tests for astra_core/reference_verification.py (RASTI protocol embedding).

Network fetchers are injected mocks — no network in tests. The mocks return
FIXTURE-like canned API payloads, not astronomical results, so the no-synthetic-
data rule (which governs discoveries) is not implicated.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reference_verification import (
    RefReport, cited_keys, clean, first_author_surname, parse_bib, verify_directory,
)


def make_dir(tmp: Path, tex: str, bib: str):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "paper.tex").write_text(tex)
    (tmp / "refs.bib").write_text(bib)
    return tmp


GOOD_CROSSREF = {
    "title": ["Deep silence: Radio properties of little red dots"],
    "volume": "693", "page": "L2",
    "issued": {"date-parts": [[2025]]},
    "author": [{"family": "Perger"}, {"family": "Fogasy"}],
}


class TestParsing(unittest.TestCase):
    def test_parse_bib_nested_braces(self):
        bib = '@article{dg26,\n  author = {{de Graaff}, A. and others},\n  title = {Little Red Dots host Black Hole Stars},\n  year = {2026},\n  note = {arXiv:2511.21820}\n}'
        e = parse_bib(Path("/tmp/_x.bib")) if False else None
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".bib", delete=False) as f:
            f.write(bib); p = f.name
        e = parse_bib(Path(p))["dg26"]
        self.assertIn("de Graaff", e["author"])
        self.assertEqual(_surname(e), "de graaff")

    def test_cited_keys_all_variants(self):
        tex = r"\citep{a}, \citet{b, c}, \citeauthor*{d}, plain text"
        self.assertEqual(cited_keys(tex), {"a", "b", "c", "d"})


def _surname(e):
    return first_author_surname(e)


class TestSweep(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_clean_entry_verifies(self):
        d = make_dir(self.tmp,
            r"See \citep{perger25}.",
            "@article{perger25,\n author={Perger, K. and others},\n title={Deep silence: Radio properties of little red dots},\n journal={A\\&A},\n volume={693},\n pages={L2},\n year={2025},\n doi={10.1051/x}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: GOOD_CROSSREF,
                               arxiv=lambda e: None)
        self.assertTrue(rep.ok, rep.summary())
        self.assertEqual(len(rep.verified), 1)

    def test_wrong_paper_doi_is_issue(self):
        d = make_dir(self.tmp,
            r"See \citep{k}.",
            "@article{k,\n author={Perger, K.},\n title={A totally unrelated title about linguistics},\n year={2025},\n doi={10.1051/x}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: GOOD_CROSSREF,
                               arxiv=lambda e: None)
        self.assertFalse(rep.ok)

    def test_doi_resolves_to_other_authors(self):
        d = make_dir(self.tmp,
            r"See \citep{k}.",
            "@article{k,\n author={Nobody, A. B.},\n title={Deep silence: Radio properties of little red dots},\n year={2025},\n doi={10.1051/x}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: GOOD_CROSSREF,
                               arxiv=lambda e: None)
        self.assertFalse(rep.ok)
        self.assertTrue(any("wrong-paper" in i for i in rep.issues))

    def test_placeholder_is_issue(self):
        d = make_dir(self.tmp,
            r"See \citep{k}.",
            "@article{k,\n author={X},\n title={[CHECK ADS]},\n year={2025}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: None,
                               arxiv=lambda e: None)
        self.assertFalse(rep.ok)
        self.assertTrue(any("placeholder" in i for i in rep.issues))

    def test_cited_but_missing(self):
        d = make_dir(self.tmp, r"See \citep{ghost}.", "@article{real,\n title={T},\n year={2025}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: None, arxiv=lambda e: None)
        self.assertFalse(rep.ok)
        self.assertTrue(any("CITED BUT NOT IN BIB" in i for i in rep.issues))

    def test_uncited_flagged_as_warning_not_issue(self):
        d = make_dir(self.tmp, r"See \citep{a}.",
            "@article{a,\n title={T},\n year={2025},\n doi={10.1/z}\n}\n@article{b,\n title={U},\n year={2025}\n}")
        rep = verify_directory(d, sleep=0,
                               crossref=lambda doi: {"title": ["T"], "volume": "",
                                                     "page": "", "issued": {"date-parts": [[2025]]},
                                                     "author": []},
                               arxiv=lambda e: None)
        self.assertTrue(rep.ok)
        self.assertTrue(any("uncited" in w for w in rep.warnings))

    def test_published_arxiv_warns(self):
        d = make_dir(self.tmp, r"See \citep{pre}.",
            "@article{pre,\n author={A, B.},\n title={Some preprint title},\n year={2026},\n note={arXiv:2501.00001}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: None,
                               arxiv=lambda e: {"title": "Some preprint title",
                                                "journal_ref": "ApJ 999, 1 (2026)"})
        self.assertTrue(rep.ok)
        self.assertTrue(any("cite the published form" in w for w in rep.warnings))

    def test_confusable_family_needs_human(self):
        d = make_dir(self.tmp, r"See \citep{a1}.",
            "@article{a1,\n author={Smith, J.},\n title={T1},\n year={2025},\n doi={10.1/a}\n}\n"
            "@article{a2,\n author={Smith, J.},\n title={T2},\n year={2025}\n}")
        rep = verify_directory(d, sleep=0,
                               crossref=lambda doi: {"title": ["T1"], "volume": "",
                                                     "page": "", "issued": {"date-parts": [[2025]]},
                                                     "author": [{"family": "Smith"}]},
                               arxiv=lambda e: None)
        self.assertTrue(rep.ok)
        self.assertTrue(any("confusable family" in h for h in rep.needs_human))

    def test_book_exempt(self):
        d = make_dir(self.tmp, r"See \citep{bk}.",
            "@book{bk,\n author={Draine, B. T.},\n title={Physics of the Interstellar and Intergalactic Medium},\n year={2011}\n}")
        rep = verify_directory(d, sleep=0, crossref=lambda doi: None, arxiv=lambda e: None)
        self.assertTrue(rep.ok)
        self.assertEqual(len(rep.exempt), 1)


if __name__ == "__main__":
    unittest.main()
